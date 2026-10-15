"""Turn what happens to remedy PRs and issues into P2 decision events (review/51 P4).

Three inputs, one output. A **webhook signal** (a remedy PR merged, closed or reopened; a remedy
issue closed or reopened), a **reconciliation** pass that recovers a signal the webhook missed, and
a maintainer's **`/remedy reopen <decision_id>`** comment all become immutable P2 ledger events.
Each is verified against what GitHub says, never against what a body claims:

* the repository is this one, and the artifact is a *recognized ledger artifact*: opened by the
  repository's own Actions identity, carrying decision markers whose bindings recompute to the
  decision ids, and, for a PR, on the branch those decisions derive (not a fork);
* a human acted: bots never produce feedback, and the actor must hold repository write access;
* every event carries a delivery id naming the exact GitHub event, so a retried delivery or a
  reconciliation that sees the same fact appends nothing new.

Meanings: a merge is the maintainer's approval (``covered``/``assigned``); closing a PR unmerged is
a rejection (``refinement_needed``/``rejected``, with the PR as the artifact a refinement issue
answers); reopening puts the decision back ``in_review``; closing an issue nobody settled holds the
decision (``blocked``/``watch``); ``/remedy reopen`` records ``proposed`` so the next publication
pass may reopen the artifact. None of them changes policy, grants an alias auto-merge, or writes
feed configuration.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from citypods.github_permissions import RepositoryPermissionError, require_repository_write
from citypods.remedy_ledger import (
    DecisionState,
    RemedyEvent,
    _assert_remedy_lease,
    append_event,
    decision_id,
    event_id,
    load_decisions,
    parse_event,
)
from citypods.remedy_queue import branch_matches, parse_bindings, parse_markers

BOT_LOGIN = "github-actions[bot]"
TERMINAL = frozenset({"covered", "excluded", "resolved"})
REOPEN_COMMAND = re.compile(r"/remedy reopen ([0-9a-f]{64})[ \t]*\r?")


class FeedbackError(ValueError):
    """The signal cannot be trusted or recorded; nothing was written."""


@dataclass(frozen=True)
class Signal:
    """A verified fact about a remedy artifact."""

    kind: str  # "pr" | "issue"
    action: str  # "merged" | "closed" | "reopened"
    number: int
    url: str
    decision_ids: tuple[str, ...]
    bindings: dict[str, dict[str, Any]]
    actor: str
    at: str  # the GitHub event time, UTC ISO: the same fact always yields the same event
    delivery: str


def _utc(value: Any, name: str) -> str:
    try:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise FeedbackError(f"{name}: invalid timestamp") from exc
    if stamp.utcoffset() is None:
        raise FeedbackError(f"{name}: expected an aware timestamp")
    return stamp.astimezone(UTC).isoformat()


def _recognize(
    artifact: Mapping[str, Any], *, kind: str, repository: str
) -> tuple[tuple[str, ...], dict[str, dict[str, Any]]]:
    """The decisions an artifact carries, or why it is not one of ours."""
    author = artifact.get("user") or {}
    if author.get("login") != BOT_LOGIN or author.get("type") != "Bot":
        raise FeedbackError("artifact was not opened by the repository's Actions identity")
    ids = parse_markers(artifact.get("body"))
    bindings = parse_bindings(artifact.get("body"))
    if not ids or set(ids) - set(bindings):
        raise FeedbackError("artifact does not carry a binding for every marked decision")
    for identity in ids:
        binding = bindings[identity]
        recomputed = decision_id(
            binding["city"],
            binding["source_key"],
            binding["policy_id"],
            binding["normalized_label"],
        )
        if recomputed != identity:
            raise FeedbackError("a binding does not recompute to its decision id")
    if kind == "pr":
        head = artifact.get("head") or {}
        if (head.get("repo") or {}).get("full_name") != repository:
            raise FeedbackError("pull request head is not in this repository")
        if not branch_matches(head.get("ref"), ids):
            raise FeedbackError("pull request branch is not the one its decisions derive")
    return ids, bindings


def signal_from_event(event_name: str, payload: Mapping[str, Any], *, repository: str) -> Signal:
    """Verify a ``pull_request_target`` or ``issues`` payload and name what happened."""
    if (payload.get("repository") or {}).get("full_name") != repository:
        raise FeedbackError("event is not for this repository")
    sender = payload.get("sender") or {}
    if sender.get("type") == "Bot" or not sender.get("login"):
        raise FeedbackError("bots do not produce remedy feedback")
    action = str(payload.get("action") or "")
    if event_name == "pull_request_target":
        artifact, kind = payload.get("pull_request") or {}, "pr"
    elif event_name == "issues":
        artifact, kind = payload.get("issue") or {}, "issue"
        if "pull_request" in artifact:
            raise FeedbackError("a pull request is not a remedy issue")
    else:
        raise FeedbackError(f"unsupported event {event_name!r}")
    if action not in ("closed", "reopened"):
        raise FeedbackError(f"unsupported action {action!r}")
    ids, bindings = _recognize(artifact, kind=kind, repository=repository)
    number = artifact.get("number")
    if type(number) is not int or number <= 0:
        raise FeedbackError("artifact number is invalid")
    merged = kind == "pr" and bool(artifact.get("merged"))
    if merged and action == "closed":
        action, at = "merged", artifact.get("merged_at")
    elif action == "closed":
        at = artifact.get("closed_at")
    else:
        at = artifact.get("updated_at")
    stamp = _utc(at, "event time")
    url = str(artifact.get("html_url") or "")
    path = "pull" if kind == "pr" else "issues"
    if url != f"https://github.com/{repository}/{path}/{number}":
        raise FeedbackError("artifact URL does not match this repository")
    return Signal(
        kind,
        action,
        number,
        url,
        ids,
        bindings,
        str(sender["login"]),
        stamp,
        f"{repository}:{kind}:{number}:{action}:{stamp}",
    )


def signal_from_snapshot(artifact: Any, *, repository: str) -> Signal | None:
    """The reconciliation form: a settled marked artifact seen in the GitHub listing.

    It is verified exactly as a webhook payload is, from the artifact's author, body and branch.
    ``None`` when the artifact is still open, was settled by a bot, or is not one of ours."""
    if artifact.state == "open" or not artifact.settled_by or artifact.settled_by.endswith("[bot]"):
        return None
    if artifact.settled_at is None:
        return None
    as_payload = {
        "user": {
            "login": artifact.actor,
            "type": "Bot" if artifact.actor.endswith("[bot]") else "User",
        },
        "body": artifact.body,
        "head": {"repo": {"full_name": artifact.head_repo}, "ref": artifact.head_ref},
    }
    try:
        ids, bindings = _recognize(as_payload, kind=artifact.kind, repository=repository)
    except FeedbackError:
        return None
    if sorted(ids) != sorted(artifact.decision_ids):
        return None
    action = "merged" if artifact.state == "merged" else "closed"
    stamp = _utc(artifact.settled_at.isoformat(), "event time")
    path = "pull" if artifact.kind == "pr" else "issues"
    return Signal(
        artifact.kind,
        action,
        artifact.number,
        f"https://github.com/{repository}/{path}/{artifact.number}",
        ids,
        bindings,
        artifact.settled_by,
        stamp,
        f"{repository}:{artifact.kind}:{artifact.number}:{action}:{stamp}",
    )


def _target(signal: Signal) -> tuple[str, str | None, str]:
    """(state, disposition, rationale) a signal records for each of its decisions."""
    if signal.kind == "pr":
        if signal.action == "merged":
            return (
                "covered",
                "assigned",
                f"Pull request #{signal.number} was merged by {signal.actor}.",
            )
        if signal.action == "closed":
            return (
                "refinement_needed",
                "rejected",
                f"Pull request #{signal.number} was closed without merging by {signal.actor}.",
            )
        return "in_review", None, f"Pull request #{signal.number} was reopened by {signal.actor}."
    if signal.action == "closed":
        return (
            "blocked",
            "watch",
            f"Issue #{signal.number} was closed by {signal.actor} without a recorded disposition.",
        )
    return "in_review", None, f"Issue #{signal.number} was reopened by {signal.actor}."


def _already_recorded(tip: DecisionState | None, signal: Signal, state: str, outcome: Any) -> bool:
    if tip is None or tip.diagnostics or len(tip.tip_ids) != 1:
        return False
    event = next(e for e in tip.events if e.event_id == tip.tip_ids[0])
    return (event.state, event.disposition, event.artifact_ref) == (state, outcome, signal.url)


def events_for_signal(signal: Signal, histories: Mapping[str, DecisionState]) -> list[RemedyEvent]:
    """One event per decision the artifact carries; none for a decision already in that state."""
    state, outcome, rationale = _target(signal)
    events: list[RemedyEvent] = []
    for identity in signal.decision_ids:
        binding = signal.bindings[identity]
        history = histories.get(identity)
        if history is not None and (history.diagnostics or len(history.tip_ids) != 1):
            raise FeedbackError(f"decision {identity[:12]} has a damaged or forked history")
        tip = None
        if history is not None:
            tip = next(e for e in history.events if e.event_id == history.tip_ids[0])
        if _already_recorded(history, signal, state, outcome):
            continue
        if tip is not None and tip.state in TERMINAL and state in ("blocked", "refinement_needed"):
            # A settled decision is not unsettled by a close: only `/remedy reopen` does that.
            continue
        approval = signal.url if state == "covered" or outcome in ("watch", "assigned") else None
        payload: dict[str, Any] = {
            "schema_version": 1,
            "city": binding["city"],
            "source_key": binding["source_key"],
            "policy_id": binding["policy_id"],
            "normalized_label": binding["normalized_label"],
            "decision_id": identity,
            "event_id": "",
            "parent_event_ids": list(history.tip_ids) if history is not None else [],
            "occurred_at": signal.at,
            "actor_kind": "maintainer",
            "actor_id": signal.actor,
            "state": state,
            "evidence_hash": binding["evidence_hash"],
            "config_hash": binding["config_hash"],
            "policy_hash": binding["policy_hash"],
            "recording_refs": binding["recording_refs"],
            "provenance": [],
            "rationale": rationale,
            "artifact_ref": signal.url,
            "approval_ref": approval,
            "external_delivery_id": signal.delivery,
            "disposition": outcome,
        }
        payload["event_id"] = event_id(payload)
        events.append(parse_event(payload))
    return events


def record_signal(storage, signal: Signal, *, lease) -> list[str]:
    """Append the signal's events under the remedy lease; returns the keys written or retried."""
    _assert_remedy_lease(lease)
    sources = sorted({signal.bindings[i]["source_key"] for i in signal.decision_ids})
    histories = load_decisions(storage, source_keys=sources)
    return [
        append_event(storage, event, lease=lease) for event in events_for_signal(signal, histories)
    ]


# ---- /remedy reopen -------------------------------------------------------------------------


def parse_reopen_command(body: Any) -> str | None:
    """The decision id a comment asks to reopen, or None when it is not that command."""
    if not isinstance(body, str):
        return None
    match = REOPEN_COMMAND.fullmatch(body.strip("\n"))
    return match.group(1) if match else None


def reopen_event(
    *,
    command_decision: str,
    comment: Mapping[str, Any],
    permission: Mapping[str, Any],
    repository: str,
    artifact: Mapping[str, Any],
    kind: str,
    history: DecisionState | None,
) -> RemedyEvent:
    """Validate a maintainer's `/remedy reopen` and build the P2 reopen event.

    The event carries the decision's own hashes unchanged: it records a request to reconsider and
    cannot change policy, evidence or an alias grant."""
    try:
        require_repository_write(dict(permission))
    except RepositoryPermissionError as exc:
        raise FeedbackError(str(exc)) from exc
    author = (comment.get("user") or {}).get("login")
    if not author or (permission.get("user") or {}).get("login") != author:
        raise FeedbackError("permission does not belong to the commenter")
    comment_id = comment.get("id")
    if type(comment_id) is not int or comment_id <= 0:
        raise FeedbackError("comment id is invalid")
    url = str(comment.get("html_url") or "")
    if not re.fullmatch(
        rf"https://github\.com/{re.escape(repository)}/(issues|pull)/[1-9][0-9]*#issuecomment-{comment_id}",
        url,
    ):
        raise FeedbackError("comment URL does not match this repository")
    ids, bindings = _recognize(artifact, kind=kind, repository=repository)
    if command_decision not in ids:
        raise FeedbackError("the artifact does not carry that decision")
    if history is None or history.diagnostics or len(history.tip_ids) != 1:
        raise FeedbackError("there is no recorded decision history to reopen")
    tip = next(e for e in history.events if e.event_id == history.tip_ids[0])
    delivery = f"{repository}:issuecomment:{comment_id}"
    for prior in history.events:
        if prior.external_delivery_id == delivery:
            return prior  # a retried delivery
    if tip.state in ("proposed", "in_review"):
        raise FeedbackError("the decision is already open for review")
    binding = bindings[command_decision]
    payload: dict[str, Any] = {
        "schema_version": 1,
        "city": tip.city,
        "source_key": tip.source_key,
        "policy_id": tip.policy_id,
        "normalized_label": tip.normalized_label,
        "decision_id": tip.decision_id,
        "event_id": "",
        "parent_event_ids": list(history.tip_ids),
        "occurred_at": _utc(comment.get("created_at"), "comment time"),
        "actor_kind": "maintainer",
        "actor_id": author,
        "state": "proposed",
        "evidence_hash": tip.evidence_hash,
        "config_hash": tip.config_hash,
        "policy_hash": tip.policy_hash,
        "recording_refs": [
            {"source_key": r.source_key, "uid": r.uid, "provider_guid": r.provider_guid}
            for r in tip.recording_refs
        ]
        or binding["recording_refs"],
        "provenance": [],
        "rationale": f"Reopened for reconsideration by {author}.",
        "artifact_ref": url,
        "approval_ref": url,
        "external_delivery_id": delivery,
        "disposition": None,
    }
    payload["event_id"] = event_id(payload)
    return parse_event(payload)


def reopen_command(
    storage,
    *,
    comment: Mapping[str, Any],
    permission: Mapping[str, Any],
    repository: str,
    artifact: Mapping[str, Any],
    kind: str,
    lease,
) -> tuple[RemedyEvent, str]:
    """The whole `/remedy reopen` path: parse, verify, build the event and append it."""
    decision = parse_reopen_command(comment.get("body"))
    if decision is None:
        raise FeedbackError("the comment is not a `/remedy reopen <decision_id>` command")
    _ids, bindings = _recognize(artifact, kind=kind, repository=repository)
    if decision not in bindings:
        raise FeedbackError("the artifact does not carry that decision")
    source = bindings[decision]["source_key"]
    history = load_decisions(storage, source_keys=[source]).get(decision)
    event = reopen_event(
        command_decision=decision,
        comment=comment,
        permission=permission,
        repository=repository,
        artifact=artifact,
        kind=kind,
        history=history,
    )
    return event, record_reopen(storage, event, lease=lease)


def record_reopen(storage, event: RemedyEvent, *, lease) -> str:
    _assert_remedy_lease(lease)
    return append_event(storage, event, lease=lease)


def reopened(history: DecisionState | None) -> bool:
    """Whether the decision's tip is an explicit reopening of a settled or rejected decision."""
    if history is None or history.diagnostics or len(history.tip_ids) != 1:
        return False
    by_id = {e.event_id: e for e in history.events}
    tip = by_id[history.tip_ids[0]]
    if tip.state != "proposed" or tip.disposition is not None or not tip.parent_event_ids:
        return False
    return any(
        by_id[p].state in TERMINAL or by_id[p].disposition == "rejected"
        for p in tip.parent_event_ids
        if p in by_id
    )


def refinement_candidates(
    histories: Mapping[str, DecisionState],
) -> Sequence[tuple[str, RemedyEvent]]:
    """Decisions whose PR was rejected and that still await a refinement issue."""
    found: list[tuple[str, RemedyEvent]] = []
    for identity, history in sorted(histories.items()):
        if history.diagnostics or len(history.tip_ids) != 1:
            continue
        tip = next(e for e in history.events if e.event_id == history.tip_ids[0])
        if tip.state == "refinement_needed" and tip.disposition == "rejected" and tip.artifact_ref:
            found.append((identity, tip))
    return found
