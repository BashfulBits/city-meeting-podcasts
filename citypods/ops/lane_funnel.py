"""LLM lane funnel table for `/admin/status` (review/58 sections 4 and 9).

Pure projection of the ``llm_work`` aggregates already stored in ``run_history`` / ``run_events``.
It reads nothing else: no Durable Object read, no ``/v2/stats`` call, no storage listing.

Events are append-only and older ones lack the funnel fields (``queue``, ``admission``,
``first_seen_at``, ``packets``); every accessor tolerates their absence, and a lane whose latest
event cannot support a statement says "not measured" instead of guessing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

WORKING = ("ready", "queued", "ingress_limited", "stopped")

# What each lane waits on when its own work is blocked (review/58 section 2 lane dependencies).
UPSTREAM = {
    "chapter-agenda": "agenda text",
    "chapter-locator": "transcripts or agenda chapters",
    "topic-tags:tagger": "chapters",
    "r6-moments": "transcripts",
    "judge:anchor": "tag and moment candidates",
    "judge:sibling": "tag and moment candidates",
    "judge:adjudicator": "judge verdicts",
    "remedy:p3": "unexpected-body findings",
}

NOT_MEASURED_DISPATCH = "not measured: dispatcher-side (Worker logs), not in producer run events"


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _parse(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _hours(delta_seconds: float) -> str:
    hours = max(0.0, delta_seconds) / 3600
    if hours < 1:
        return "<1 h"
    if hours < 48:
        return f"{hours:.0f} h"
    return f"{hours / 24:.0f} d"


def lane_events(history: list[dict], purpose: str) -> list[tuple[datetime, dict]]:
    """Events that reported ``purpose``, oldest first, as ``(event time, purpose row)``."""
    out = []
    for event in history:
        work = event.get("llm_work") if isinstance(event, dict) else None
        purposes = work.get("purposes") if isinstance(work, dict) else None
        row = purposes.get(purpose) if isinstance(purposes, dict) else None
        when = _parse(event.get("ts"))
        if isinstance(row, dict) and when is not None:
            out.append((when, row))
    out.sort(key=lambda pair: pair[0])
    return out


def _states(row: dict) -> dict:
    return row["states"] if isinstance(row.get("states"), dict) else {}


def _backlog(row: dict) -> int:
    return sum(_int(_states(row).get(name)) for name in WORKING)


def _backlog_since(events: list[tuple[datetime, dict]]) -> datetime | None:
    """Start of the unbroken run of newest events that still held a backlog.

    A lower bound on how long the oldest eligible item has waited: items older than the first
    event of the streak are invisible to run-local telemetry. Uses the earliest of the event time
    and any ``oldest_eligible_at`` the event carried.
    """
    since = None
    for when, row in reversed(events):
        if _backlog(row) == 0:
            break
        candidates = [when]
        # Only the eligible timestamp: first_seen_at also covers consumed items.
        seen = _parse(row.get("oldest_eligible_at"))
        if seen is not None:
            candidates.append(seen)
        since = min([since, *candidates]) if since else min(candidates)
    return since


def _text(head: str, body: str, backlog: int, unit: str, partial: str) -> str:
    text = f"{head}; {body}" if head else body
    return f"{text}; eligible backlog {backlog} {unit}s{partial}"


def classify(purpose: str, row: dict) -> tuple[str, str]:
    """Return ``(class_code, summary)`` for one purpose row (review/58 section 4 classes).

    Codes: ``a`` input-starved, ``b`` producer-limited, ``c`` dispatcher-limited,
    ``d`` return-path-limited, ``e`` budget-capped or policy-held, ``idle`` caught up,
    ``unknown`` not measured.
    """
    states = _states(row)
    ready, queued = _int(states.get("ready")), _int(states.get("queued"))
    limited, stopped = _int(states.get("ingress_limited")), _int(states.get("stopped"))
    blocked, policy_held = _int(states.get("blocked")), _int(states.get("policy_held"))
    errored = _int(states.get("errored"))
    backlog = ready + queued + limited + stopped
    unit = row.get("unit") if isinstance(row.get("unit"), str) else "item"
    upstream = UPSTREAM.get(purpose, "upstream input")
    partial = " (partial run)" if row.get("coverage") == "partial" else ""
    if _int(row.get("observed")) == 0:
        return "unknown", "nothing observed in the latest event"
    if backlog == 0:
        if errored:
            return "e", f"{errored} {unit}s errored, none eligible{partial}"
        if policy_held:
            return "e", f"held by policy: {policy_held} {unit}s{partial}"
        if blocked:
            return "a", f"waiting on {upstream} ({blocked} {unit}s blocked){partial}"
        if row.get("coverage") == "partial" or row.get("scope") == "sample":
            return "unknown", "no backlog seen, but the event is a partial run or research sample"
        return "idle", f"caught up{partial}"
    # Most known work is not yet eligible: say so, then classify the eligible remainder.
    head = f"waiting on {upstream} ({blocked} blocked)" if blocked > backlog else ""
    if limited and limited >= max(ready, queued, stopped):
        body = f"ingress budget closed, {limited} {unit}s deferred"
        return "e", _text(head, body, backlog, unit, partial)
    if ready + stopped > queued:
        body = f"{ready + stopped} eligible {unit}s not submitted this run"
        return ("a" if head else "b"), _text(head, body, backlog, unit, partial)
    queue = row.get("queue") if isinstance(row.get("queue"), dict) else None
    if queue is None:
        body = f"{queued} queued; Worker/collection split not measured"
        return "unknown", _text(head, body, backlog, unit, partial)
    waiting = _int(queue.get("waiting_at_worker"))
    done = _int(queue.get("completed_uncollected"))
    failed = _int(queue.get("failed_at_worker"))
    if _int(queue.get("unobserved")) >= max(waiting, done, 1):
        body = f"{queued} queued; producers did not poll, so waiting vs completed is not measured"
        return "unknown", _text(head, body, backlog, unit, partial)
    if failed > max(waiting, done):
        body = f"{failed} failed at the Worker, awaiting retry"
        return "c", _text(head, body, backlog, unit, partial)
    if done > waiting:
        body = f"{done} completed at the Worker, not collected"
        return "d", _text(head, body, backlog, unit, partial)
    return "c", _text(head, f"{waiting} waiting at the Worker", backlog, unit, partial)


def _counts(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {str(key): _int(count) for key, count in value.items()}


def lane_rows(
    history: list[dict], purposes: list[str], *, now: datetime | None = None
) -> list[dict]:
    """One row per configured purpose, newest event wins; absent data says so."""
    now = now or datetime.now(UTC)
    rows = []
    for purpose in purposes:
        events = lane_events(history, purpose)
        base = {
            "purpose": purpose,
            "slot_productivity": NOT_MEASURED_DISPATCH,
            "failed_attempt_share": NOT_MEASURED_DISPATCH,
        }
        if not events:
            rows.append(
                {
                    **base,
                    "class": "unknown",
                    "summary": "not measured: no run event reports this lane yet",
                    "event_ts": None,
                }
            )
            continue
        when, row = events[-1]
        code, summary = classify(purpose, row)
        since = _backlog_since(events)
        if since is not None:
            summary += f"; backlog seen for at least {_hours((now - since).total_seconds())}"
        rows.append(
            {
                **base,
                "class": code,
                "summary": summary,
                "event_ts": when.isoformat(),
                "unit": row.get("unit"),
                "coverage": row.get("coverage"),
                "scope": row.get("scope"),
                "observed": _int(row.get("observed")),
                "backlog": _backlog(row),
                "states": _counts(row.get("states")) or {},
                "queue": _counts(row.get("queue")),
                "admission": _counts(row.get("admission")),
                "packets": _counts(row.get("packets")),
                "backlog_since": since.isoformat() if since else None,
            }
        )
    return rows
