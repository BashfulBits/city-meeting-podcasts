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
import zlib
from collections.abc import Callable, Mapping, Sequence
from glob import escape
from typing import Any

from citypods.provider_catalog.reconcile import Report, _merge_into_last_full
from citypods.review_issues import (
    DECISION_BLOCK_END,
    DECISION_BLOCK_START,
    bounded_body,
    checked_decisions,
)

ISSUE_TITLE = "Provider catalog: pending decisions"
ISSUE_LABEL = "provider-catalog"
MARKER = "<!-- citypods:provider-catalog v=3 -->"
LEGACY_MARKERS = tuple(f"<!-- citypods:provider-catalog v={v} -->" for v in (1, 2))


def is_catalog_issue(body: str) -> bool:
    return any(marker in body for marker in (MARKER, *LEGACY_MARKERS))


_STATE_RE = re.compile(r"<!-- citypods:provider-catalog-state ([A-Za-z0-9+/=]+) -->")
# Leave room for the state marker after the bounded human-readable part.
_HUMAN_BODY_LIMIT = 52_000

Runner = Callable[[Sequence[str]], str]


def decode_state(body: str) -> dict[str, Any]:
    match = _STATE_RE.search(body or "")
    if not match:
        return {}
    try:
        raw = base64.b64decode(match.group(1))
        if not raw.startswith(b"{"):
            decoder = zlib.decompressobj()
            raw = decoder.decompress(raw, 1_000_000)
            if not decoder.eof:
                return {}
        state = json.loads(raw)
    except (ValueError, TypeError, zlib.error):
        return {}
    return state if isinstance(state, dict) else {}


def _encode_state(state: Mapping[str, Any]) -> str:
    raw = base64.b64encode(
        zlib.compress(json.dumps(state, sort_keys=True, separators=(",", ":")).encode())
    ).decode()
    return f"<!-- citypods:provider-catalog-state {raw} -->"


# A free route that turns paid is never removed automatically (maintainer decision 2026-09-24):
# it becomes a decision here instead, applied by `/apply` like any other.
PAID_ROUTE_VERDICT = "not_entitled"


def decision_choices(report: Report) -> tuple[str, ...]:
    choices: list[str] = []
    for candidate in report.candidates:
        key = f"`{candidate.key}`"
        choices.append(f"{key}: ignore")
        choices.append(f"{key}: add route only")
        choices.extend(f"{key}: add as backup to `{lane}`" for lane in candidate.lanes)
    for anomaly in report.anomalies:
        if anomaly.verdict == PAID_ROUTE_VERDICT:
            choices.append(f"`{anomaly.route_id}`: remove route")
            choices.append(f"`{anomaly.route_id}`: keep as a paid route")
    choices.extend(c.choice for c in report.rate_changes if c.action == "offer_increase")
    from citypods.provider_catalog.limits import context_choice

    choices.extend(context_choice(c) for c in report.context_changes)
    return tuple(choices)


def _render_decision_block(choices: Sequence[str], checked: set[str]) -> str:
    lines = [f"- [{'x' if c in checked else ' '}] {c}" for c in choices]
    return "\n".join([DECISION_BLOCK_START, *lines, DECISION_BLOCK_END])


def _score_cell(score: float | None, below_floor: bool | None) -> str:
    if score is None:
        return "unscored"
    return f"{score:g}" + (" (below floor)" if below_floor else "")


def fulfilled_choices(previous_body, limits, lanes, decisions, today) -> set[str]:
    """Fulfillment is read from main's config, never an open PR or issue state assertion."""
    snapshot = Report()
    try:
        _merge_into_last_full(snapshot, decode_state(previous_body).get("last_full") or {}, set())
    except (TypeError, ValueError, KeyError):
        return set()
    fulfilled = set()
    for candidate in snapshot.candidates:
        prefix = f"`{candidate.key}`: "
        if decisions.is_ignored(candidate.provider, candidate.model, today):
            fulfilled.update(c for c in decision_choices(snapshot) if c.startswith(prefix))
            continue
        pools = {
            str(r.get("model_key") or r["model"])
            for r in limits.get("routes") or []
            if r.get("provider") == candidate.provider
            and r.get("upstream_model") == candidate.model
            and r.get("free") is True
            and r.get("rpd") != 0
        }
        if len(pools) != 1:
            continue
        model_key = next(iter(pools))
        fulfilled.add(prefix + "add route only")
        fulfilled.update(
            prefix + f"add as backup to `{lane}`"
            for lane in candidate.lanes
            if lane in lanes and model_key in lanes[lane].backup_models
        )
    for anomaly in snapshot.anomalies:
        if anomaly.verdict != PAID_ROUTE_VERDICT:
            continue
        route = next(
            (r for r in limits.get("routes") or [] if r.get("route_id") == anomaly.route_id), None
        )
        if (
            route
            and route.get("provider") == anomaly.provider
            and route.get("upstream_model") == anomaly.model
            and route.get("free") is False
            and any(
                entry.get("provider") == anomaly.provider
                and entry.get("model_glob") == escape(anomaly.model)
                and entry.get("verdict") == PAID_ROUTE_VERDICT
                for entry in decisions.acknowledged
            )
        ):
            fulfilled.add(f"`{anomaly.route_id}`: keep as a paid route")
    for change in snapshot.rate_changes:
        block = (
            next((r for r in limits.get("routes") or [] if r.get("route_id") == change.target), {})
            if change.scope == "route"
            else (limits.get("providers") or {}).get(change.target, {})
        )
        if (
            change.scope in {"route", "provider"}
            and change.metric in {"rpm", "tpm", "rpd"}
            and change.action == "offer_increase"
            and (change.scope != "route" or block.get("provider") == change.provider)
            and (change.scope != "provider" or change.target == change.provider)
            and block.get(change.metric) == change.new
        ):
            fulfilled.add(change.choice)
    from citypods.provider_catalog.limits import context_choice

    for change in snapshot.context_changes:
        rid, field, _old, new, _stamp = change
        route = next((r for r in limits.get("routes", []) if r.get("route_id") == rid), None)
        if route and route.get(field) == new:
            fulfilled.add(context_choice(change))
    return fulfilled


def render_body(
    report: Report, *, run_date: str, previous_body: str = "", fulfilled: set[str] | None = None
) -> str:
    fulfilled = fulfilled or set()
    choices = tuple(c for c in decision_choices(report) if c not in fulfilled)
    prior = Report()
    try:
        _merge_into_last_full(prior, decode_state(previous_body).get("last_full") or {}, set())
    except (TypeError, ValueError, KeyError):
        prior = Report()
    prior_choices = decision_choices(prior)
    selected = set(checked_decisions(previous_body, prior_choices + choices)) - fulfilled
    pending = [
        c
        for c in prior.candidates
        if c.key not in {current.key for current in report.candidates}
        and any(choice.startswith(f"`{c.key}`: ") for choice in selected)
    ]
    pending_paid = [
        a
        for a in prior.anomalies
        if a.verdict == PAID_ROUTE_VERDICT
        and a.key not in {current.key for current in report.anomalies}
        and any(choice.startswith(f"`{a.route_id}`: ") for choice in selected)
    ]
    pending_rates = [
        c
        for c in prior.rate_changes
        if c.choice in selected and c.choice not in choices and c.choice not in fulfilled
    ]
    choices += tuple(c.choice for c in pending_rates)
    from citypods.provider_catalog.limits import context_choice

    pending_context = [
        c
        for c in prior.context_changes
        if context_choice(c) in selected
        and context_choice(c) not in choices
        and context_choice(c) not in fulfilled
    ]
    choices += tuple(context_choice(c) for c in pending_context)
    if pending or pending_paid:
        pending_choices = decision_choices(Report(candidates=pending, anomalies=pending_paid))
        choices += tuple(c for c in pending_choices if c not in choices and c not in fulfilled)
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
            "| Model | AA index | Context | JSON method | Proven | "
            "Other lanes (gap to weakest) | Research |",
            "|---|---|---|---|---|---|---|",
        ]
        for c in report.candidates:
            links = " · ".join(f"[{label}]({url})" for label, url in c.links)
            context = f"{c.context_limit:,}" if c.context_limit else "unlisted"
            others = ", ".join(f"{lane} (-{gap:g})" for lane, gap in c.other_lanes) or "-"
            lines.append(
                f"| `{c.key}` | {_score_cell(c.score, c.below_floor)} | {context} | "
                f"{c.structured_output_method or 'unverified'} | {c.proven_on} | "
                f"{others} | {links} |"
            )
        lines += [
            "",
            "Decide below in **Decisions**. A lane checkbox "
            "is offered when the candidate scores at least that lane's weakest scored model; "
            "other eligible lanes are listed with the gap (capacity backups are often weaker by "
            "design). Promoting a lane's primary model stays a manual edit.",
        ]
    else:
        lines.append("None this week.")
    if pending:
        names = ", ".join(f"`{c.key}`" for c in pending)
        lines += [
            "",
            f"Pending prior selections: {names}. Their previous proofs are advisory; "
            "selected decisions stay below until fulfilled on main or changed by a maintainer.",
        ]
    if pending_paid:
        names = ", ".join(f"`{a.route_id}`" for a in pending_paid)
        lines += [
            "",
            f"Pending prior paid-route selections: {names}. Prior anomaly evidence is "
            "advisory; selections remain until fulfilled on main or changed by a maintainer.",
        ]
    lines += ["", f"## Configured-route anomalies ({len(report.anomalies)})", ""]
    if report.anomalies:
        for a in report.anomalies:
            flag = "**new** " if a.new else ""
            where = ", absent from catalog" if a.absent_from_catalog else ""
            lines.append(
                f"- {flag}`{a.route_id}` (`{a.model}`): **{a.verdict}** -- {a.reason}{where}"
            )
            if not a.lane_usage:
                lines.append("  - used by no LLM lane")
            for lane, alternates in a.lane_usage:
                still = (
                    ", ".join(f"`{m}`" for m in alternates) or "**nothing else -- the lane stalls**"
                )
                lines.append(f"  - used by `{lane}`; still served by: {still}")
        lines += [
            "",
            "Fix the route in `config/provider_limits.yml`, or record the state as known under "
            "`acknowledged:` in `config/provider_catalog_decisions.yml`.",
        ]
    else:
        lines.append("None.")
    if report.rate_changes or pending_rates:
        lines += ["", "## Rate maintenance", ""]
        for change in (*report.rate_changes, *pending_rates):
            lines.append(
                f"- {change.scope} `{change.target}` {change.metric}: {change.old} → {change.new}"
                + (" (material tightening greater than 50%)" if change.material else "")
            )
        lines += [
            "",
            "Tightening is prepared from verified evidence. Increases require a selected choice. "
            "Every preparation re-fetches successful workflow artifacts; "
            "these values are advisory.",
        ]
    # The decision block must survive truncation whole: the next weekly update reads ticks back
    # from it (checked_decisions), so a cut block would silently reset them. Only the report above
    # it is truncated; observations are dropped to a stub before the block is touched.
    decisions = ""
    if choices:
        decisions = "\n".join(
            [
                "",
                "## Decisions",
                "",
                "Tick what you want, then comment `/apply` to prepare reviewed catalog/rate PRs "
                "with those changes (review/48). Ticks are kept across weekly updates. A free "
                "route that became paid is never removed automatically: remove it, or keep it as "
                "a paid route. Additions/ignore and keep-paid choices are available; "
                "removal requires deployed Slice 3 rescue. Unavailable proofs remain ticked. "
                "An open PR does not fulfill a "
                "selection until it is merged.",
                "",
                _render_decision_block(choices, checked),
            ]
        )
    summary = f"<details><summary>Observations ({len(report.observations)})</summary>"
    observations = "\n".join(
        ["", summary, "", *[f"- {o}" for o in report.observations], "", "</details>", ""]
    )
    state = dict(report.state)
    if pending or pending_paid or pending_rates or pending_context:
        from dataclasses import asdict

        state["last_full"] = dict(state.get("last_full") or {})
        state["last_full"]["candidates"] = [asdict(c) for c in (*report.candidates, *pending)]
        state["last_full"]["anomalies"] = [asdict(a) for a in (*report.anomalies, *pending_paid)]
        state["last_full"]["context_changes"] = list((*report.context_changes, *pending_context))
        state["last_full"]["rate_changes"] = [
            asdict(c) for c in (*report.rate_changes, *pending_rates)
        ]
    state_marker = _encode_state(state)
    room = min(_HUMAN_BODY_LIMIT, 65_000 - len(state_marker.encode("utf-8")))
    room -= len(decisions.encode("utf-8"))
    if len(observations.encode("utf-8")) > room // 2:
        observations = f"\n{summary}\n\nOmitted: the issue body limit was reached.\n\n</details>\n"
    report_text, _ = bounded_body(
        "\n".join(lines) + "\n", limit=max(0, room - len(observations.encode("utf-8")))
    )
    human = report_text.rstrip("\n") + "\n" + decisions + "\n" + observations
    return f"{human}\n{state_marker}\n"


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
        if is_catalog_issue(issue.get("body") or ""):
            return issue
    return None


def _report_from_body(body):
    report = Report()
    try:
        _merge_into_last_full(report, decode_state(body).get("last_full") or {}, set())
    except (TypeError, ValueError, KeyError):
        pass
    return report


def sync_issue(
    report: Report,
    *,
    run_date: str,
    run: Runner = _gh,
    limits=None,
    lanes=None,
    decisions=None,
    today=None,
) -> str:
    """Create, update, close or reopen the managed issue. Returns a one-line action summary."""
    existing = find_issue(run)
    previous_body = (existing or {}).get("body") or ""
    fulfilled = (
        fulfilled_choices(previous_body, limits, lanes, decisions, today)
        if limits is not None and lanes is not None and decisions is not None
        else set()
    )
    body = render_body(report, run_date=run_date, previous_body=previous_body, fulfilled=fulfilled)
    # Selected pending choices remain actionable even when discovery no longer lists the route.
    actionable = report.actionable or bool(
        checked_decisions(body, decision_choices(_report_from_body(body)))
    )
    if existing is None:
        if not actionable:
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
    if actionable and not is_open:
        run(["issue", "reopen", number])
        return f"reopened #{number}"
    if not actionable and is_open:
        run(["issue", "close", number, "--comment", "Nothing actionable this week."])
        return f"closed #{number}"
    return f"updated #{number}"
