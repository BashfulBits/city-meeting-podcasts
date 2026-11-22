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
import statistics
import subprocess
import zlib
from collections import Counter
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
# Smallest human-readable share worth keeping before the display-only state is trimmed.
_MIN_HUMAN_BODY = 12_000

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


def method_choice(route_id: str, method: str) -> str:
    return f"`{route_id}`: switch structured-output method to `{method}`"


def aa_match_choice(candidate_key: str, match: Mapping[str, Any]) -> str:
    """Checkbox text that accepts one AA model as a candidate's AA entry (next run uses it)."""
    label = re.sub(r"[^A-Za-z0-9 ._+-]", "", str(match.get("name") or ""))[:60].strip()
    score = match.get("score")
    detail = ", ".join(
        part
        for part in (label, f"index {score:g}" if isinstance(score, (int, float)) else "")
        if part
    )
    return f"`{candidate_key}`: AA match `{match['creator']}/{match['slug']}`" + (
        f" ({detail})" if detail else ""
    )


def accepted_aa_matches(body: str, state: Mapping[str, Any]) -> dict[str, tuple[str, str]]:
    """Candidate key -> (AA creator, AA slug): matches kept in state plus rows ticked in the issue.

    A ticked row counts only when it is exactly one of the closest matches the last run offered
    for that candidate, so editing the issue cannot point a model at an arbitrary AA entry.
    """
    stored = state.get("aa_matches")
    accepted = {
        str(key): (str(value[0]), str(value[1]))
        for key, value in (stored.items() if isinstance(stored, Mapping) else ())
        if isinstance(value, (list, tuple)) and len(value) == 2
    }
    snapshot = Report()
    try:
        _merge_into_last_full(snapshot, state.get("last_full") or {}, set())
    except (TypeError, ValueError, KeyError):
        return accepted
    offered = {
        aa_match_choice(c.key, m): (c.key, (m["creator"], m["slug"]))
        for c in snapshot.candidates
        for m in c.aa_matches or ()
    }
    for choice in checked_decisions(body, tuple(offered)):
        key, target = offered[choice]
        accepted[key] = target
    return accepted


ADD = "add"
IGNORE = "ignore"
REMOVE = "remove"

# Ticks made under the earlier (v3) wording still count: `migrate_ticks` rewrites them to the
# add/ignore/remove choices, so a maintainer never has to re-tick after the format changed.
_LEGACY_TICKS = (
    (re.compile(r"^(`[^`]+`): add route only$"), rf"\1: {ADD}"),
    (re.compile(r"^(`[^`]+`): add as backup to `[^`]+`$"), rf"\1: {ADD}"),
    (re.compile(r"^(`[^`]+`): remove route$"), rf"\1: {REMOVE}"),
    (re.compile(r"^(`[^`]+`): keep as a paid route$"), rf"\1: {IGNORE}"),
)
# Choices the issue no longer offers (lane placement, AA matching and re-tests moved out of it).
_RETIRED_TICKS = re.compile(r"^`[^`]+`: (AA match .*|test again)$")


def migrate_ticks(body: str) -> str:
    """Rewrite ticked rows from the old wording inside the decision block; drop retired ones.

    Several old rows can map to one new choice (`add route only` plus a lane backup both mean
    `add`), so a rewritten row that repeats a choice is dropped. A row ticked twice in the current
    wording is left alone, so `/apply` still rejects it as a duplicate.
    """
    text = (body or "").replace("\r\n", "\n")
    start = text.rfind(DECISION_BLOCK_START)
    end = text.find(DECISION_BLOCK_END, start) if start != -1 else -1
    if start == -1 or end == -1:
        return text
    seen: set[str] = set()
    out = []
    for line in text[start:end].split("\n"):
        match = re.fullmatch(r"- \[[xX]\] (.*?)\s*", line)
        if match:
            original = choice = match.group(1)
            for pattern, replacement in _LEGACY_TICKS:
                choice = pattern.sub(replacement, choice)
            if _RETIRED_TICKS.match(choice) or (choice != original and choice in seen):
                continue
            seen.add(choice)
            if choice != original:
                line = f"- [x] {choice}"
        out.append(line)
    return text[:start] + "\n".join(out) + text[end:]


def decision_choices(report: Report) -> tuple[str, ...]:
    """Every candidate is `add` or `ignore`; a free route that turned paid is `remove` or `ignore`.

    Lane placement is not offered: a new route joins its model pool, and lane membership stays a
    reviewed manual edit. `ignore` on a paid route keeps it as a paid route (acknowledged).
    """
    choices: list[str] = []
    for candidate in report.candidates:
        key = f"`{candidate.key}`"
        choices += [f"{key}: {ADD}", f"{key}: {IGNORE}"]
    for anomaly in report.anomalies:
        if anomaly.verdict == PAID_ROUTE_VERDICT:
            choices.append(f"`{anomaly.route_id}`: {REMOVE}")
            choices.append(f"`{anomaly.route_id}`: {IGNORE}")
    for anomaly in report.anomalies:
        if anomaly.verdict == "structured_output_invalid" and anomaly.alternative_method:
            choices.append(method_choice(anomaly.route_id, anomaly.alternative_method))
    choices.extend(c.choice for c in report.rate_changes if c.action == "offer_increase")
    from citypods.provider_catalog.limits import context_choice

    choices.extend(context_choice(c) for c in report.context_changes)
    return tuple(choices)


def _group_key(candidate) -> str:
    """One section per model: the same model offered by several providers shares a key."""
    name = candidate.model.lower().rsplit("/", 1)[-1]
    return re.sub(r"(:free|-free)$", "", name)


def _anomaly_next(anomaly) -> str:
    """What happens to this anomaly, or what the maintainer can do about it."""
    if anomaly.verdict in {"retired", "not_served"}:
        from citypods.provider_catalog.retire import RETIREMENTS_ENABLED

        if not RETIREMENTS_ENABLED:
            return "automatic removal PRs are not enabled; remove the route by hand"
        stalls = any(not alternates for _lane, alternates in anomaly.lane_usage)
        if stalls:
            return (
                "no automatic removal while a lane would stall; add a replacement model, or "
                "alias the retired ID, then the next daily run removes the route"
            )
        return (
            "no tick needed -- the next daily run re-probes this route and opens a removal PR "
            "once two independent checks agree (lanes are repaired in that PR)"
        )
    if anomaly.verdict == PAID_ROUTE_VERDICT:
        return "tick `remove`, or `ignore` to keep it as a paid route"
    if anomaly.verdict == "structured_output_invalid":
        if anomaly.alternative_method:
            return (
                f"tick the switch to `{anomaly.alternative_method}` under **Decisions**; "
                "`/apply` re-verifies it on the route before proposing the change"
            )
        return "no other method verified; fix the route in `config/provider_limits.yml`"
    return (
        "fix the route in `config/provider_limits.yml`, or record the state as known under "
        "`acknowledged:` in `config/provider_catalog_decisions.yml`"
    )


def _short(value) -> str:
    """A token count at a glance: 1.05M, 128k, 900."""
    if type(value) is not int or value <= 0:
        return "?"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if value >= 1_000:
        return f"{round(value / 1_000)}k"
    return str(value)


def _quality_line(candidate, floor: float | None, ceiling: float | None) -> str:
    """What a maintainer judges a model by: its AA index against the bar and the lanes we run."""
    score = candidate.score
    if score is None:
        text = "AA **unscored**"
        match = next(iter(candidate.aa_matches or ()), None)
        if isinstance(match, Mapping) and isinstance(match.get("score"), (int, float)):
            name = _inline(match.get("name") or match.get("slug"), 60)
            text += f" (closest AA entry: {name}, {match['score']:g})"
    else:
        text = f"AA **{score:g}**"
        if floor is not None and score <= floor:
            text += f", at or below the bar ({floor:g})"
        elif floor is not None and ceiling is not None:
            text += f" vs our lanes' {floor:g}–{ceiling:g}"
        elif floor is not None:
            text += f", above the bar ({floor:g})"
    if candidate.lanes:
        text += " · good enough for " + ", ".join(f"`{lane}`" for lane in candidate.lanes)
    elif candidate.other_lanes:
        text += " · short of " + ", ".join(
            f"`{lane}` by {gap:g}" for lane, gap in candidate.other_lanes[:3]
        )
    facts = candidate.facts or {}
    parts = (
        facts.get("params") or facts.get("size"),
        f"{_short(candidate.context_limit)} context" if candidate.context_limit else None,
        f"released {facts['released']}" if facts.get("released") else None,
    )
    for part in parts:
        if part:
            text += f" · {_inline(part, 40)}"
    if facts.get("expires"):
        text += f" · **free tier ends {_inline(facts['expires'], 24)}**"
    return text


_STATE_LABEL = {"ready": "ready", "waiting": "testing", "action": "needs you"}


def _route_line(candidate) -> str:
    """One provider's offer of the model: whether `/apply` can add it now, and its tested caps."""
    from citypods.provider_catalog.readiness import READY, candidate_readiness

    readiness = candidate_readiness(candidate)
    caps = (
        f"in {_short(readiness.input_cap)} / out {_short(readiness.output_cap)}"
        if readiness.input_cap or readiness.output_cap
        else "caps not tested yet"
    )
    if readiness.output_source == "provisional":
        caps += " (provisional)"
    method = candidate.structured_output_method or "unverified"
    line = (
        f"`{candidate.provider}` · **{_STATE_LABEL[readiness.state]}** · {caps} · "
        f"JSON {method} · tested {candidate.proven_on}"
    )
    if readiness.state != READY:
        line += f" — {_inline(readiness.next_step, 160)}"
    return line


def _links(members) -> str:
    card = next(((c.facts or {}).get("card") for c in members if (c.facts or {}).get("card")), None)
    links = [("model card", card)] if card else []
    links += list(dict.fromkeys(link for c in members for link in c.links))
    return " · ".join(f"[{label}]({url})" for label, url in links[:2])


def _render_decision_block(
    choices: Sequence[str],
    checked: set[str],
    candidates=(),
    anomalies=(),
    floor: float | None = None,
    ceiling: float | None = None,
) -> str:
    """Checkboxes grouped by model; the rows are exactly the choices, only prose is added.

    Only the `- [ ] choice` lines are machine-read (checked_decisions/parse_decisions), so any
    heading or description between them is inert. Each model takes a heading line with its
    quality, one line per provider offering it, and that provider's add/ignore boxes.
    """

    def row(choice: str) -> str:
        return f"- [{'x' if choice in checked else ' '}] {choice}"

    out = [DECISION_BLOCK_START]
    placed: set[str] = set()
    groups: dict[str, list] = {}
    for candidate in candidates:
        groups.setdefault(_group_key(candidate), []).append(candidate)
    for members in groups.values():
        rows = {c.key: [x for x in choices if x.startswith(f"`{c.key}`: ")] for c in members}
        if not any(rows.values()):
            continue
        lead = members[0]
        name = re.sub(r"(:free|-free)$", "", lead.model)
        heading = f"**`{name}`** — {_quality_line(lead, floor, ceiling)}"
        links = _links(members)
        out += ["", heading + (f" · {links}" if links else "")]
        summary = next(
            ((c.facts or {}).get("summary") for c in members if (c.facts or {}).get("summary")),
            "",
        )
        if summary:
            out.append(f"<sub>{_inline(summary, 160)}</sub>")
        for candidate in members:
            if not rows[candidate.key]:
                continue
            out += ["", _route_line(candidate)]
            out += [row(x) for x in rows[candidate.key]]
            placed.update(rows[candidate.key])
    leftover = [x for x in choices if x not in placed]
    if leftover:
        by_route = {a.route_id: a for a in anomalies}
        out += ["", "**Route maintenance**"]
        current = None
        for choice in leftover:
            match = re.match(r"`([^`]+)`: ", choice)
            route = by_route.get(match.group(1)) if match else None
            if route is not None and route.route_id != current:
                current = route.route_id
                lanes = ", ".join(
                    f"`{lane}`" + ("" if alternates else " (it would stall)")
                    for lane, alternates in route.lane_usage
                )
                out += [
                    "",
                    f"`{route.route_id}` ({route.verdict}): {_inline(route.reason, 160)}"
                    + (f" · used by {lanes}" if lanes else " · used by no lane"),
                ]
            elif route is None and current is not None:
                current = None
                out.append("")
            out.append(row(choice))
    out.append(DECISION_BLOCK_END)
    return "\n".join(out)


def _readiness_line(candidate) -> str:
    from citypods.provider_catalog.readiness import candidate_readiness

    readiness = candidate_readiness(candidate)
    return f"{readiness.headline} {readiness.next_step}"


def _inline(value, limit: int = 240) -> str:
    """Untrusted one-line text: no markup, links, mentions or line breaks."""
    text = re.sub(r"[\r\n\t]+", " ", str(value or ""))
    text = re.sub(r"[<>`|\[\]@*~#]", "", text)
    return text.strip()[:limit]


def _score_cell(score: float | None, below_floor: bool | None) -> str:
    if score is None:
        return "unscored"
    return f"{score:g}" + (" (below floor)" if below_floor else "")


def _discovery_count(value) -> str:
    return f"{value:,}" if type(value) is int else "—"


def _discovery_transport_groups(value):
    """Keep editable issue state from injecting unbounded or malformed telemetry into reports."""
    if not isinstance(value, list):
        return ()
    groups = []
    for group in value[:8]:
        if not isinstance(group, list) or not 2 <= len(group) <= 5:
            continue
        normalized = []
        for row in group:
            if not isinstance(row, Mapping):
                continue
            axis = row.get("axis")
            outcome = row.get("outcome")
            diagnostic = row.get("diagnostic")
            attempt_id = row.get("attempt_id")
            if not isinstance(axis, str) or axis not in {
                "input",
                "output",
                "total_input_heavy",
                "total_output_heavy",
            }:
                axis = "unknown"
            if not isinstance(outcome, str) or outcome not in {
                "admitted",
                "refused",
                "quota",
                "daily",
                "outage",
                "inconclusive",
                "lower_only",
            }:
                outcome = "unknown"
            if not isinstance(diagnostic, str) or not re.fullmatch(
                r"[A-Za-z0-9_.-]{1,64}", diagnostic
            ):
                diagnostic = "unknown"
            if not isinstance(attempt_id, str) or not re.fullmatch(r"[a-f0-9]{64}", attempt_id):
                attempt_id = "unavailable"
            normalized.append(
                {
                    "axis": axis,
                    "target": _discovery_count(row.get("target")),
                    "reserved_input": _discovery_count(row.get("reserved_input")),
                    "requested_output": _discovery_count(row.get("requested_output")),
                    "outcome": outcome,
                    "diagnostic": diagnostic,
                    "attempt_id": attempt_id[:12],
                }
            )
        if len(normalized) >= 2:
            groups.append(normalized)
    return tuple(groups)


def _discovery_attempt_diagnostics(value):
    """Render only bounded, typed response metadata from the editable issue marker."""
    if not isinstance(value, list):
        return ()
    rows = []
    choices = {
        "axis": {"input", "output", "total_input_heavy", "total_output_heavy"},
        "outcome": {
            "admitted",
            "refused",
            "quota",
            "daily",
            "outage",
            "inconclusive",
            "lower_only",
        },
        "message_content_state": {
            "unknown",
            "not_applicable",
            "missing",
            "null",
            "empty",
            "text",
            "other",
        },
        "finish_reason_state": {
            "unknown",
            "not_applicable",
            "missing",
            "null",
            "stop",
            "length",
            "other",
        },
        "usage_state": {
            "unknown",
            "not_applicable",
            "missing",
            "not_object",
            "prompt_missing",
            "prompt_invalid",
            "completion_missing",
            "completion_invalid",
            "total_invalid",
            "total_missing",
            "valid",
        },
        "response_media_type": {"unknown", "application/json", "text/event-stream", "other"},
        "sentinel_verification": {"unknown", "passed", "failed", "not_checked"},
    }
    for item in value[-12:]:
        if not isinstance(item, Mapping):
            continue
        row = dict(item)
        for name, allowed in choices.items():
            if not isinstance(row.get(name), str) or row.get(name) not in allowed:
                row[name] = "unknown"
        diagnostic = row.get("diagnostic")
        row["diagnostic"] = (
            diagnostic
            if isinstance(diagnostic, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", diagnostic)
            else "unknown"
        )
        for name in (
            "target",
            "reserved_input",
            "requested_output",
            "response_bytes",
            "reported_input",
            "reported_output",
        ):
            number = row.get(name)
            row[name] = number if type(number) is int and 0 <= number <= 4 * 1024 * 1024 else None
        for name, bound in (("stream_bytes", 64 * 1024 * 1024), ("stream_events", 1_000_000)):
            value = row.get(name)
            row[name] = value if type(value) is int and 0 <= value <= bound else None
        status = row.get("response_status")
        row["response_status"] = status if type(status) is int and 100 <= status <= 599 else None
        for name in ("provider_request_id", "response_id"):
            value = row.get(name)
            row[name] = (
                value
                if isinstance(value, str)
                and 1 <= len(value) <= 128
                and re.fullmatch(r"[A-Za-z0-9._:-]+", value)
                else ""
            )
        finish = row.get("finish_reason")
        row["finish_reason"] = (
            finish
            if isinstance(finish, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,64}", finish)
            else ""
        )
        preview = row.get("response_preview")
        try:
            decoded = (
                json.loads(preview) if isinstance(preview, str) and len(preview) <= 1_024 else None
            )
        except (TypeError, ValueError):
            decoded = None
        if isinstance(decoded, str) and len(decoded) <= 128:
            safe = json.dumps(decoded, ensure_ascii=True)
            row["response_preview"] = (
                safe.replace("`", r"\u0060")
                .replace("@", r"\u0040")
                .replace("<", r"\u003c")
                .replace(">", r"\u003e")
                .replace("[", r"\u005b")
                .replace("]", r"\u005d")
                .replace("(", r"\u0028")
                .replace(")", r"\u0029")
                .replace("!", r"\u0021")
                .replace("#", r"\u0023")
                .replace("|", r"\u007c")
                .replace("*", r"\u002a")
                .replace("_", r"\u005f")
                .replace("~", r"\u007e")
            )
        else:
            row["response_preview"] = ""
        rows.append(row)
    return tuple(rows)


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
        # The route is on main: the model is no longer new, so neither choice is shown again.
        fulfilled.update((prefix + ADD, prefix + IGNORE))
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
            fulfilled.add(f"`{anomaly.route_id}`: {IGNORE}")
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


def _remeasure_cost_lines(ran, waiting) -> list[str]:
    """What the sessions cost and why the others waited, so the next change can be sized."""
    out = []
    timed = [s for s in ran if isinstance(s.get("seconds"), int)]
    if timed:
        confirms = sum(1 for s in ran if s.get("confirmed"))
        calls = sum(s["calls"] for s in ran if isinstance(s.get("calls"), int))
        seconds = sorted(s["seconds"] for s in timed)
        median = round(statistics.median(seconds))
        out.append(
            f"- Sessions took {sum(seconds) // 60} min in all (median {median} s, longest "
            f"{seconds[-1]} s), {calls} provider calls; {confirms} confirmed a known bracket "
            "instead of searching."
        )
    reasons = Counter(str(s.get("note") or "not run") for s in waiting)
    if reasons:
        top = "; ".join(f"{n} {text}" for text, n in reasons.most_common(5))
        out.append(f"- Waiting, by reason: {top}.")
    return out


def _remeasure_lines(remeasure) -> list[str]:
    """Configured-route cap re-measurement (review/54 §8.14): offers, pending cuts, waits."""
    if not isinstance(remeasure, dict):
        return []
    details = remeasure.get("details") or {}
    pending = remeasure.get("pending") or []
    sessions = remeasure.get("sessions") or []
    if not (details or pending or sessions):
        return []
    lines = ["**Configured route caps**", ""]
    for detail in details.values():
        dates = ", ".join(s.get("date", "?") for s in detail.get("sessions") or [])
        lines.append(
            f"- `{detail['route_id']}` {detail['field']}: {detail['old']} → {detail['new']} "
            f"({detail['kind']}; measured {dates})"
        )
    for row in pending:
        lines.append(
            f"- `{row['route_id']}` {row['axis']}: refused below the current cap "
            f"({row['cap']}) on {row['date']}; reduction pending confirmation (next weekly run)"
        )
    ran = [s for s in sessions if s.get("ran")]
    waiting = [s for s in sessions if not s.get("ran")]
    if ran or waiting:
        lines.append(
            f"- This run re-measured {len(ran)} route axis/axes; {len(waiting)} wait"
            + (
                f" (e.g. `{waiting[0]['route_id']}`: {waiting[0].get('note', 'not run')})"
                if waiting
                else ""
            )
            + "."
        )
        lines += _remeasure_cost_lines(ran, waiting)
    lines.append("")
    return lines


def render_body(
    report: Report, *, run_date: str, previous_body: str = "", fulfilled: set[str] | None = None
) -> str:
    fulfilled = fulfilled or set()
    previous_body = migrate_ticks(previous_body)
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
    ceiling = getattr(report, "ceiling", None)
    if report.quality_error:
        quality_note = (
            f"**Quality data unavailable: {_inline(report.quality_error)}.** Every model shows "
            "as unscored until this is fixed."
        )
    elif report.floor is not None:
        span = f"{report.floor:g}–{ceiling:g}" if ceiling is not None else f"{report.floor:g}+"
        quality_note = (
            f"Quality is the [Artificial Analysis](https://artificialanalysis.ai/) Intelligence "
            f"Index; the models our lanes run score {span}, and models at or below "
            f"{report.floor:g} are skipped automatically."
        )
    else:
        quality_note = "No quality bar: AA scores none of the models our lanes run."
    # A carried candidate whose route has since landed on main has no choice left: not new.
    listed = [
        c
        for c in (*report.candidates, *pending)
        if any(choice.startswith(f"`{c.key}`: ") for choice in choices)
    ]
    groups = len({_group_key(c) for c in listed})
    lines = [
        MARKER,
        f"**Provider catalog, {run_date}:** {groups} new model(s) from "
        f"{len(listed)} provider route(s), "
        f"{len(report.anomalies)} route problem(s). {quality_note}",
        "",
    ]
    problems = [a for a in report.anomalies if a.verdict != PAID_ROUTE_VERDICT]
    if problems:
        lines += [f"**Route problems ({len(problems)})** — no tick; the next step is noted.", ""]
        for a in problems:
            where = ", absent from catalog" if a.absent_from_catalog else ""
            stalls = [lane for lane, alternates in a.lane_usage if not alternates]
            stall = f" **Stalls `{'`, `'.join(stalls)}`.**" if stalls else ""
            lines.append(
                f"- `{a.route_id}`: **{a.verdict}**{where} — {_inline(a.reason, 160)}.{stall} "
                f"Next: {_anomaly_next(a)}"
            )
        lines.append("")
    if report.rate_changes or pending_rates:
        lines += ["**Rate changes**", ""]
        for change in (*report.rate_changes, *pending_rates):
            lines.append(
                f"- {change.scope} `{change.target}` {change.metric}: {change.old} → {change.new}"
                + (" (tightening greater than 50%)" if change.material else "")
            )
        lines.append("")
    lines += _remeasure_lines(report.state.get("remeasure"))
    # The decision block must survive truncation whole: the next update reads ticks back from it
    # (checked_decisions), so a cut block would silently reset them. Only the text above it is
    # ever truncated.
    decisions = ""
    if choices:
        decisions = "\n".join(
            [
                "",
                "## Decisions",
                "",
                "Tick **add** or **ignore** for each new model (per provider), **remove** or "
                "**ignore** (keep as a paid route) for a route that stopped being free, then "
                "comment `/apply`. **ready** routes are added with their tested input/output "
                "caps to the model's own pool; lane placement stays a manual edit. **testing** "
                "routes are still being measured by the daily run: a tick on one stays until "
                "it is ready. `/apply` replies with what it did and why anything waited.",
                "",
                _render_decision_block(
                    choices,
                    checked,
                    [*report.candidates, *pending],
                    [*report.anomalies, *pending_paid],
                    report.floor,
                    ceiling,
                ),
            ]
        )
    observations = (
        f"\n<sub>{len(report.observations)} run observation(s) are in the reconcile workflow "
        "log.</sub>\n"
        if report.observations
        else ""
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
    if state.get("bulk_ref"):
        # The bulk memory is kept in the run's artifact; the issue holds only what decisions use.
        from citypods.provider_catalog.state_store import split_bulk

        state = split_bulk(state)[0]
    state_marker = _encode_state(state)
    if 65_000 - len(state_marker.encode("utf-8")) < _MIN_HUMAN_BODY:
        # The machine state alone filled the issue body, leaving no room for the report, and the
        # truncation then dropped the leading MARKER (2026-10-30: #2554 grew to 61 KB of state and
        # every marker-gated workflow skipped). Observations are carried only to be displayed, so
        # shed them before anything a decision depends on.
        last_full = dict(state.get("last_full") or {})
        if last_full.pop("observations", None) is not None:
            state["last_full"] = last_full
            state_marker = _encode_state(state)
    room = min(_HUMAN_BODY_LIMIT, 65_000 - len(state_marker.encode("utf-8")))
    room -= len(decisions.encode("utf-8"))
    if len(observations.encode("utf-8")) > room // 2:
        observations = ""
    report_text, _ = bounded_body(
        "\n".join(lines) + "\n", limit=max(0, room - len(observations.encode("utf-8")))
    )
    if MARKER not in report_text:
        # Truncation must never take the marker: `find_issue` and the /apply workflow key on it.
        report_text = f"{MARKER}\n{report_text}"
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
