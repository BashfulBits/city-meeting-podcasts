"""Bounded publication planning for remedy decisions (review/51 P4).

Pure and provider-free: given the decisions ready to publish, the P2 ledger's view of each, and a
snapshot of the remedy artifacts already on GitHub, :func:`plan_publications` says what to open,
what to update, what to reopen and what to leave queued, within the configured budgets. It performs
no write; the publisher executes a plan, and a report-only run prints it for inspection first.

What it enforces (``config/remedy.yml`` ``limits``):

* a PR packs at most ``decisions_per_pr`` decisions of the same city and body family with a
  compatible approval outcome; a new body or family never shares a PR with an automatic alias;
* at most ``prs_per_rolling_week`` PRs are *newly opened or reopened* in the preceding seven days
  and at most ``open_prs`` remedy PRs are open at once, counting every marked artifact whoever
  opened it; updating an existing artifact consumes no slot, closing or merging one frees an open
  slot but never resets the rolling count;
* at most ``active_directional_issues`` directional issues are active;
* work is scheduled oldest first per city, round-robin across cities, so one city's backlog cannot
  starve another's; whatever does not fit stays queued with its reason and age, never merged into
  an oversized PR and never dropped;
* a decision is never published twice: a marked open artifact is updated, a closed or merged one is
  left alone unless a reopen was explicitly requested, and a decision whose current evidence was
  rejected or already settled in the ledger does not regenerate.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

MARKER_PREFIX = "citypods:remedy:v1:"
_MARKER = re.compile(r"<!-- citypods:remedy:v1:([0-9a-f]{64}) -->")
# P3's first issues carried this key instead; they name the same decision, so they still count.
_LEGACY_MARKER = re.compile(r"<!-- citypods:remedy-p3:key=([0-9a-f]{64}) -->")
ROLLING_WINDOW = timedelta(days=7)
TERMINAL_STATES = frozenset({"covered", "excluded", "resolved"})

Kind = Literal["pr", "issue"]


def marker(decision_id: str) -> str:
    """The stable body marker naming one decision an artifact carries."""
    if not re.fullmatch(r"[0-9a-f]{64}", decision_id or ""):
        raise ValueError("decision id must be a lowercase SHA-256")
    return f"<!-- {MARKER_PREFIX}{decision_id} -->"


def render_markers(decision_ids: Iterable[str]) -> str:
    """One marker per participating decision, sorted, for an artifact body."""
    return "\n".join(marker(item) for item in sorted(set(decision_ids)))


def parse_markers(body: str | None) -> tuple[str, ...]:
    """The decision ids an artifact body carries markers for (sorted, unique)."""
    text = body or ""
    return tuple(sorted(set(_MARKER.findall(text)) | set(_LEGACY_MARKER.findall(text))))


BINDING_FIELDS = (
    "city",
    "source_key",
    "policy_id",
    "normalized_label",
    "evidence_hash",
    "config_hash",
    "policy_hash",
    "recording_refs",
)
_BINDING = re.compile(r"<!-- citypods:remedy:binding:v1:([0-9a-f]{64}):([A-Za-z0-9_=-]+) -->")


def render_binding(decision_id: str, binding: Mapping[str, Any]) -> str:
    """The facts a ledger event for ``decision_id`` needs, carried in the artifact's own body.

    A PR or issue closed or merged later is turned into a decision event from this binding, so the
    artifact is self-describing and the feedback path needs no copy of the evidence."""
    marker(decision_id)
    missing = [name for name in BINDING_FIELDS if name not in binding]
    if missing:
        raise ValueError(f"binding is missing {missing}")
    payload = json.dumps(
        {name: binding[name] for name in BINDING_FIELDS}, sort_keys=True, separators=(",", ":")
    )
    token = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"<!-- citypods:remedy:binding:v1:{decision_id}:{token} -->"


def parse_bindings(body: str | None) -> dict[str, dict[str, Any]]:
    """Every well-formed binding in a body, by decision id; malformed ones are left out."""
    found: dict[str, dict[str, Any]] = {}
    for decision, token in _BINDING.findall(body or ""):
        try:
            value = json.loads(base64.urlsafe_b64decode(token.encode()).decode())
        except (ValueError, UnicodeError):
            continue
        if isinstance(value, dict) and set(value) == set(BINDING_FIELDS):
            found[decision] = value
    return found


def branch_matches(head_ref: str | None, decision_ids: Iterable[str]) -> bool:
    """Whether a head ref is the branch these decisions derive (the hash binds the set)."""
    if not head_ref or not re.fullmatch(r"fix/remedy-[a-z0-9-]+-[0-9a-f]{12}", head_ref):
        return False
    return head_ref.rsplit("-", 1)[-1] == group_hash(decision_ids)


def slug_component(value: str, *, name: str, limit: int = 40) -> str:
    """A branch-safe component: lower-case ASCII words joined by hyphens, never empty."""
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")[:limit].strip("-")
    if not slug:
        raise ValueError(f"{name} has no branch-safe characters")
    return slug


def group_hash(decision_ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(set(decision_ids))).encode()).hexdigest()[:12]


def branch_name(city: str, family: str, decision_ids: Iterable[str]) -> str:
    """``fix/remedy-<city>-<family>-<group_hash>``, derived from the unit ids, not the evidence."""
    ids = sorted(set(decision_ids))
    if not ids:
        raise ValueError("a branch needs at least one decision")
    for item in ids:
        marker(item)  # validates the shape
    name = (
        f"fix/remedy-{slug_component(city, name='city')}-"
        f"{slug_component(family, name='family')}-{group_hash(ids)}"
    )
    if not re.fullmatch(r"fix/remedy-[a-z0-9-]+-[0-9a-f]{12}", name):
        raise ValueError(f"derived branch {name!r} is not valid")
    return name


@dataclass(frozen=True)
class Candidate:
    """One decision ready to publish."""

    decision_id: str
    city: str
    family: str
    outcome: str  # approval outcomes only share a PR when equal
    kind: Kind
    eligible_since: datetime
    evidence_hash: str = ""
    new_body: bool = False  # a body or family the catalog has not seen
    automatic_alias: bool = False
    reopen_requested: bool = False  # `/remedy reopen <decision_id>` by a maintainer
    refines: str | None = None  # the rejected PR a refinement issue answers


@dataclass(frozen=True)
class LedgerView:
    """The P2 ledger's current state for one decision."""

    state: str
    disposition: str | None
    evidence_hash: str
    # The tip is an explicit reopening (`/remedy reopen`) of a settled or rejected decision.
    reopened: bool = False
    # What the tip event refers to: the rejected PR, for a refinement issue to answer.
    artifact_ref: str | None = None


@dataclass(frozen=True)
class Artifact:
    """A remedy PR or issue already on GitHub, carrying decision markers."""

    kind: Kind
    number: int
    url: str
    state: Literal["open", "closed", "merged"]
    opened_at: datetime
    decision_ids: tuple[str, ...]
    reopened_at: tuple[datetime, ...] = ()
    head_ref: str | None = None
    actor: str = ""  # who opened it
    # What reconciliation needs to recognize and settle an artifact like a webhook would.
    body: str = ""
    head_repo: str = ""
    settled_by: str = ""  # who merged or closed it
    settled_at: datetime | None = None


@dataclass(frozen=True)
class GitHubSnapshot:
    """Every marked artifact, whoever opened it. ``complete`` is False when the listing was cut
    off, in which case nothing may be reserved against it."""

    artifacts: tuple[Artifact, ...]
    complete: bool = True


@dataclass(frozen=True)
class PlannedPR:
    branch: str
    city: str
    family: str
    outcome: str
    decision_ids: tuple[str, ...]


@dataclass(frozen=True)
class PlannedIssue:
    decision_id: str
    city: str
    refines: str | None = None


@dataclass(frozen=True)
class Update:
    """An existing open artifact that keeps its slot and takes the new state of its decisions."""

    artifact: Artifact
    decision_ids: tuple[str, ...]


@dataclass(frozen=True)
class Reopen:
    """A reopening: counted as an admission against the budget like a new artifact."""

    artifact: Artifact
    decision_ids: tuple[str, ...]


@dataclass(frozen=True)
class Queued:
    decision_id: str
    city: str
    kind: Kind
    reason: str
    eligible_since: datetime
    age_days: float


@dataclass(frozen=True)
class Skipped:
    decision_id: str
    reason: str


@dataclass(frozen=True)
class Budget:
    """The counts the plan was made against and what it leaves."""

    opened_in_window: int
    open_prs: int
    active_issues: int
    pr_slots: int
    issue_slots: int
    weekly_limit: int
    open_limit: int
    issue_limit: int


@dataclass(frozen=True)
class PublicationPlan:
    prs: tuple[PlannedPR, ...] = ()
    issues: tuple[PlannedIssue, ...] = ()
    updates: tuple[Update, ...] = ()
    reopens: tuple[Reopen, ...] = ()
    queued: tuple[Queued, ...] = ()
    skipped: tuple[Skipped, ...] = ()
    budget: Budget | None = None
    notes: tuple[str, ...] = field(default=())


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _age_days(since: datetime, now: datetime) -> float:
    return round(max(0.0, (_aware(now) - _aware(since)).total_seconds()) / 86400, 2)


def round_robin(candidates: Sequence[Candidate]) -> list[Candidate]:
    """Oldest first within a city, one per city per round, cities ordered by their oldest entry."""
    by_city: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in sorted(
        candidates, key=lambda c: (_aware(c.eligible_since), c.city, c.decision_id)
    ):
        by_city[candidate.city].append(candidate)
    ordered: list[Candidate] = []
    while by_city:
        heads = sorted(
            ((queue[0], city) for city, queue in by_city.items()),
            key=lambda pair: (_aware(pair[0].eligible_since), pair[1], pair[0].decision_id),
        )
        for head, city in heads:
            ordered.append(head)
            by_city[city].pop(0)
            if not by_city[city]:
                del by_city[city]
    return ordered


def _compatible(pr: dict[str, Any], candidate: Candidate) -> bool:
    return (
        pr["city"] == candidate.city
        and pr["family"] == candidate.family
        and pr["outcome"] == candidate.outcome
        # A new body or family never shares a PR with an automatic alias.
        and not (candidate.new_body and pr["alias"])
        and not (candidate.automatic_alias and pr["new_body"])
    )


def plan_publications(
    candidates: Sequence[Candidate],
    ledger: Mapping[str, LedgerView],
    snapshot: GitHubSnapshot,
    *,
    limits: Any,
    now: datetime,
) -> PublicationPlan:
    """What to publish now within the budgets (see the module docstring)."""
    now = _aware(now)
    if not snapshot.complete:
        # An incomplete listing could hide artifacts that already hold slots: reserve nothing.
        return PublicationPlan(
            queued=tuple(
                Queued(
                    c.decision_id,
                    c.city,
                    c.kind,
                    "github_snapshot_incomplete",
                    c.eligible_since,
                    _age_days(c.eligible_since, now),
                )
                for c in round_robin(candidates)
            ),
            notes=("the GitHub snapshot was incomplete, so no slot was reserved",),
        )

    window_start = now - ROLLING_WINDOW
    prs = [a for a in snapshot.artifacts if a.kind == "pr"]
    issues = [a for a in snapshot.artifacts if a.kind == "issue"]
    opened = sum(1 for a in prs if _aware(a.opened_at) >= window_start) + sum(
        1 for a in prs for stamp in a.reopened_at if _aware(stamp) >= window_start
    )
    open_prs = sum(1 for a in prs if a.state == "open")
    active_issues = sum(1 for a in issues if a.state == "open")
    weekly_left = max(0, limits.prs_per_rolling_week - opened)
    open_left = max(0, limits.open_prs - open_prs)
    issue_slots = max(0, limits.active_directional_issues - active_issues)
    budget = Budget(
        opened,
        open_prs,
        active_issues,
        min(weekly_left, open_left),
        issue_slots,
        limits.prs_per_rolling_week,
        limits.open_prs,
        limits.active_directional_issues,
    )

    by_decision: dict[str, list[Artifact]] = defaultdict(list)
    for artifact in snapshot.artifacts:
        for item in artifact.decision_ids:
            by_decision[item].append(artifact)

    skipped: list[Skipped] = []
    updates: dict[int, list[str]] = defaultdict(list)
    update_artifacts: dict[int, Artifact] = {}
    reopen_ids: dict[int, list[Candidate]] = defaultdict(list)
    reopen_artifacts: dict[int, Artifact] = {}
    fresh: list[Candidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        identity = (candidate.decision_id, candidate.kind)
        if identity in seen:
            skipped.append(Skipped(candidate.decision_id, "duplicate_candidate"))
            continue
        seen.add(identity)
        view = ledger.get(candidate.decision_id)
        same_evidence = view is not None and view.evidence_hash == candidate.evidence_hash
        if view is not None and same_evidence:
            if view.state in TERMINAL_STATES:
                skipped.append(Skipped(candidate.decision_id, "terminal_disposition_current"))
                continue
            if view.disposition == "rejected" and candidate.refines is None:
                # A rejection of this exact evidence does not regenerate a PR; it needs new
                # evidence. (A refinement issue answering the rejection is the way forward.)
                skipped.append(Skipped(candidate.decision_id, "rejected_unchanged_evidence"))
                continue
        existing = [
            a for a in by_decision.get(candidate.decision_id, []) if a.kind == candidate.kind
        ]
        open_one = next((a for a in existing if a.state == "open"), None)
        if open_one is not None:
            updates[open_one.number].append(candidate.decision_id)
            update_artifacts[open_one.number] = open_one
            continue
        closed_one = next((a for a in existing if a.state in ("closed", "merged")), None)
        if closed_one is not None:
            if candidate.reopen_requested and closed_one.state == "closed":
                reopen_ids[closed_one.number].append(candidate)
                reopen_artifacts[closed_one.number] = closed_one
            else:
                skipped.append(Skipped(candidate.decision_id, f"already_{closed_one.state}"))
            continue
        fresh.append(candidate)

    queued: list[Queued] = []

    def defer(candidate: Candidate, reason: str) -> None:
        queued.append(
            Queued(
                candidate.decision_id,
                candidate.city,
                candidate.kind,
                reason,
                candidate.eligible_since,
                _age_days(candidate.eligible_since, now),
            )
        )

    # Reopening a PR is an admission; it is made before new work and takes a slot like new work.
    reopens: list[Reopen] = []
    for number, ids in sorted(reopen_ids.items()):
        artifact = reopen_artifacts[number]
        slots = min(weekly_left, open_left) if artifact.kind == "pr" else issue_slots
        if slots <= 0:
            for candidate in ids:
                defer(candidate, "reopen_budget")
            continue
        if artifact.kind == "pr":
            weekly_left -= 1
            open_left -= 1
        else:
            issue_slots -= 1
        reopens.append(Reopen(artifact, tuple(sorted(c.decision_id for c in ids))))

    planned_prs: list[dict[str, Any]] = []
    planned_issues: list[PlannedIssue] = []
    for candidate in round_robin(fresh):
        if candidate.kind == "issue":
            if issue_slots > 0:
                issue_slots -= 1
                planned_issues.append(
                    PlannedIssue(candidate.decision_id, candidate.city, candidate.refines)
                )
            else:
                defer(candidate, "issue_capacity")
            continue
        target = next(
            (
                pr
                for pr in planned_prs
                if len(pr["ids"]) < limits.decisions_per_pr and _compatible(pr, candidate)
            ),
            None,
        )
        if target is None:
            if weekly_left <= 0 or open_left <= 0:
                defer(candidate, "rolling_week_pr_budget" if weekly_left <= 0 else "open_pr_cap")
                continue
            weekly_left -= 1
            open_left -= 1
            target = {
                "city": candidate.city,
                "family": candidate.family,
                "outcome": candidate.outcome,
                "ids": [],
                "alias": False,
                "new_body": False,
            }
            planned_prs.append(target)
        target["ids"].append(candidate.decision_id)
        target["alias"] = target["alias"] or candidate.automatic_alias
        target["new_body"] = target["new_body"] or candidate.new_body

    return PublicationPlan(
        prs=tuple(
            PlannedPR(
                branch_name(pr["city"], pr["family"], pr["ids"]),
                pr["city"],
                pr["family"],
                pr["outcome"],
                tuple(sorted(pr["ids"])),
            )
            for pr in planned_prs
        ),
        issues=tuple(planned_issues),
        updates=tuple(
            Update(update_artifacts[number], tuple(sorted(ids)))
            for number, ids in sorted(updates.items())
        ),
        reopens=tuple(reopens),
        queued=tuple(queued),
        skipped=tuple(skipped),
        budget=budget,
    )


def plan_report(plan: PublicationPlan) -> dict[str, Any]:
    """The plan as plain data, for the report-only run's artifact and Actions summary."""
    budget = plan.budget
    return {
        "budget": None
        if budget is None
        else {
            "opened_in_window": budget.opened_in_window,
            "weekly_limit": budget.weekly_limit,
            "open_prs": budget.open_prs,
            "open_limit": budget.open_limit,
            "active_issues": budget.active_issues,
            "issue_limit": budget.issue_limit,
            "pr_slots_before": budget.pr_slots,
            "issue_slots_before": budget.issue_slots,
        },
        "new_prs": [
            {
                "branch": pr.branch,
                "city": pr.city,
                "family": pr.family,
                "outcome": pr.outcome,
                "decisions": list(pr.decision_ids),
            }
            for pr in plan.prs
        ],
        "new_issues": [
            {"decision": i.decision_id, "city": i.city, "refines": i.refines} for i in plan.issues
        ],
        "updates": [
            {"url": u.artifact.url, "kind": u.artifact.kind, "decisions": list(u.decision_ids)}
            for u in plan.updates
        ],
        "reopens": [
            {"url": r.artifact.url, "kind": r.artifact.kind, "decisions": list(r.decision_ids)}
            for r in plan.reopens
        ],
        "queued": [
            {
                "decision": q.decision_id,
                "city": q.city,
                "kind": q.kind,
                "reason": q.reason,
                "age_days": q.age_days,
            }
            for q in plan.queued
        ],
        "skipped": [{"decision": s.decision_id, "reason": s.reason} for s in plan.skipped],
        "notes": list(plan.notes),
    }


def parse_snapshot(payload: Mapping[str, Any]) -> GitHubSnapshot:
    """Build a snapshot from a GraphQL ``search`` result of marked PRs and issues.

    ``payload`` is ``data.search`` as returned by GitHub: ``nodes`` of ``PullRequest`` and ``Issue``
    objects with ``number``, ``url``, ``state``, ``createdAt``, ``body``, ``headRefName`` (PRs),
    ``author.login`` and ``timelineItems.nodes[].createdAt`` for reopen events. The snapshot is
    incomplete when the search reports a further page or more results than were listed."""
    artifacts: list[Artifact] = []
    nodes = payload.get("nodes") or []
    for node in nodes:
        if not isinstance(node, Mapping) or "number" not in node:
            continue
        ids = parse_markers(node.get("body"))
        if not ids:
            continue
        kind: Kind = "pr" if "headRefName" in node else "issue"
        state = str(node.get("state") or "").lower()
        if state == "merged" or node.get("mergedAt"):
            state = "merged"
        elif state not in ("open", "closed"):
            continue
        events = [
            item
            for item in ((node.get("timelineItems") or {}).get("nodes") or [])
            if isinstance(item, Mapping) and item.get("createdAt")
        ]

        def _when(value: Any) -> datetime:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

        reopened = tuple(
            _when(item["createdAt"]) for item in events if item.get("__typename") != "ClosedEvent"
        )
        closes = [item for item in events if item.get("__typename") == "ClosedEvent"]
        last_close = max(closes, key=lambda item: item["createdAt"], default=None)
        if state == "merged":
            settled_by = str((node.get("mergedBy") or {}).get("login") or "")
            settled_at = _when(node["mergedAt"]) if node.get("mergedAt") else None
        elif state == "closed" and last_close is not None:
            settled_by = str((last_close.get("actor") or {}).get("login") or "")
            settled_at = _when(last_close["createdAt"])
        else:
            settled_by, settled_at = "", None
        artifacts.append(
            Artifact(
                kind=kind,
                number=int(node["number"]),
                url=str(node.get("url") or ""),
                state=state,  # type: ignore[arg-type]
                opened_at=datetime.fromisoformat(str(node["createdAt"]).replace("Z", "+00:00")),
                decision_ids=ids,
                reopened_at=reopened,
                head_ref=node.get("headRefName"),
                actor=str((node.get("author") or {}).get("login") or ""),
                body=str(node.get("body") or ""),
                head_repo=str((node.get("headRepository") or {}).get("nameWithOwner") or ""),
                settled_by=settled_by,
                settled_at=settled_at,
            )
        )
    info = payload.get("pageInfo") or {}
    total = payload.get("issueCount")
    complete = not info.get("hasNextPage") and (not isinstance(total, int) or total <= len(nodes))
    return GitHubSnapshot(tuple(artifacts), complete)
