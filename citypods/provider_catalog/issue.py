"""The one rolling GitHub issue: render it from a Report, and create/update/close/reopen it.

Noise rules (review/48 R5): the issue lists only what a human can act on -- proven candidates and
unacknowledged anomalies on configured routes -- with everything else in a collapsed
Observations block. It closes when nothing is actionable and reopens (the same issue, found by
label + marker) when something is, so its history and the run state marker survive.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from citypods.provider_catalog.reconcile import Report
from citypods.review_issues import (
    DECISION_BLOCK_END,
    DECISION_BLOCK_START,
    bounded_body,
    checked_decisions,
)

ISSUE_TITLE = "Provider catalog: pending decisions"
ISSUE_LABEL = "provider-catalog"
MARKER = "<!-- citypods:provider-catalog v=2 -->"
_STATE_RE = re.compile(r"<!-- citypods:provider-catalog-state ([A-Za-z0-9+/=]+) -->")
# Leave room for the state marker after the bounded human-readable part.
_HUMAN_BODY_LIMIT = 52_000

Runner = Callable[[Sequence[str]], str]


def decode_state(body: str) -> dict[str, Any]:
    match = _STATE_RE.search(body or "")
    if not match:
        return {}
    try:
        state = json.loads(base64.b64decode(match.group(1)))
    except (ValueError, TypeError):
        return {}
    return state if isinstance(state, dict) else {}


def _encode_state(state: Mapping[str, Any]) -> str:
    raw = base64.b64encode(json.dumps(state, sort_keys=True).encode()).decode()
    return f"<!-- citypods:provider-catalog-state {raw} -->"


def decision_choices(report: Report) -> tuple[str, ...]:
    choices: list[str] = []
    for candidate in report.candidates:
        key = f"`{candidate.key}`"
        choices.append(f"{key}: ignore")
        choices.append(f"{key}: add route only")
        choices.extend(f"{key}: add as backup to `{lane}`" for lane in candidate.lanes)
    return tuple(choices)


def _render_decision_block(choices: Sequence[str], checked: set[str]) -> str:
    lines = [f"- [{'x' if c in checked else ' '}] {c}" for c in choices]
    return "\n".join([DECISION_BLOCK_START, *lines, DECISION_BLOCK_END])


def _score_cell(score: float | None, below_floor: bool | None) -> str:
    if score is None:
        return "unscored"
    return f"{score:g}" + (" (below floor)" if below_floor else "")


def render_body(report: Report, *, run_date: str, previous_body: str = "") -> str:
    choices = decision_choices(report)
    checked = set(checked_decisions(previous_body, choices)) if previous_body else set()
    floor = f"{report.floor:g}" if report.floor is not None else "unavailable"
    lines = [
        MARKER,
        f"Weekly provider-catalog reconciliation, {run_date} (review/48). Quality data: "
        f"[Artificial Analysis](https://artificialanalysis.ai/) Intelligence Index; floor "
        f"(lower of GPT-OSS-120B and Nemotron-3 Super) = {floor}.",
        "",
        f"## Candidates ({len(report.candidates)})",
        "",
    ]
    if report.candidates:
        lines += [
            "Free-marked models that completed a canary on our account and are not configured.",
            "",
            "| Model | AA index | Context | Proven | Other lanes (gap to weakest) | Research |",
            "|---|---|---|---|---|---|",
        ]
        for c in report.candidates:
            links = " · ".join(f"[{label}]({url})" for label, url in c.links)
            context = f"{c.context_limit:,}" if c.context_limit else "unlisted"
            others = ", ".join(f"{lane} (-{gap:g})" for lane, gap in c.other_lanes) or "-"
            lines.append(
                f"| `{c.key}` | {_score_cell(c.score, c.below_floor)} | {context} | "
                f"{c.proven_on} | {others} | {links} |"
            )
        lines += [
            "",
            "Tick what you want, then comment `/apply` to get one curated PR with exactly those "
            "changes (review/48 Slice 2). Ticks are kept across weekly updates. A lane checkbox "
            "is offered when the candidate scores at least that lane's weakest scored model; "
            "other eligible lanes are listed with the gap (capacity backups are often weaker by "
            "design). Promoting a lane's primary model stays a manual edit.",
            "",
            _render_decision_block(choices, checked),
        ]
    else:
        lines.append("None this week.")
    lines += ["", f"## Configured-route anomalies ({len(report.anomalies)})", ""]
    if report.anomalies:
        for a in report.anomalies:
            flag = "**new** " if a.new else ""
            where = ", absent from catalog" if a.absent_from_catalog else ""
            lines.append(
                f"- {flag}`{a.route_id}` (`{a.model}`): **{a.verdict}** -- {a.reason}{where}"
            )
        lines += [
            "",
            "Fix the route in `config/provider_limits.yml`, or record the state as known under "
            "`acknowledged:` in `config/provider_catalog_decisions.yml`.",
        ]
    else:
        lines.append("None.")
    lines += [
        "",
        f"<details><summary>Observations ({len(report.observations)})</summary>",
        "",
        *[f"- {o}" for o in report.observations],
        "",
        "</details>",
    ]
    human, _ = bounded_body("\n".join(lines) + "\n", limit=_HUMAN_BODY_LIMIT)
    return f"{human}\n{_encode_state(report.state)}\n"


def _gh(args: Sequence[str]) -> str:
    return subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout.strip()


def find_issue(run: Runner = _gh) -> dict[str, Any] | None:
    """The single managed issue, open or closed: most recent labeled issue carrying the marker."""
    listed = json.loads(
        run(
            [
                "issue",
                "list",
                "--label",
                ISSUE_LABEL,
                "--state",
                "all",
                "--limit",
                "20",
                "--json",
                "number,state,body",
            ]
        )
        or "[]"
    )
    for issue in listed:
        if MARKER in (issue.get("body") or ""):
            return issue
    return None


def sync_issue(report: Report, *, run_date: str, run: Runner = _gh) -> str:
    """Create, update, close or reopen the managed issue. Returns a one-line action summary."""
    existing = find_issue(run)
    body = render_body(report, run_date=run_date, previous_body=(existing or {}).get("body") or "")
    if existing is None:
        if not report.actionable:
            return "no issue: nothing actionable"
        run(
            [
                "label",
                "create",
                ISSUE_LABEL,
                "--color",
                "1d76db",
                "--force",
                "--description",
                "Provider catalog reconciliation (review/48)",
            ]
        )
        url = run(
            ["issue", "create", "--title", ISSUE_TITLE, "--label", ISSUE_LABEL, "--body", body]
        )
        return f"created {url}"
    number = str(existing["number"])
    run(["issue", "edit", number, "--body", body])
    is_open = str(existing.get("state", "")).upper() == "OPEN"
    if report.actionable and not is_open:
        run(["issue", "reopen", number])
        return f"reopened #{number}"
    if not report.actionable and is_open:
        run(["issue", "close", number, "--comment", "Nothing actionable this week."])
        return f"closed #{number}"
    return f"updated #{number}"
