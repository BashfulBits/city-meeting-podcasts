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


def decision_choices(report: Report) -> tuple[str, ...]:
    choices: list[str] = []
    for candidate in report.candidates:
        key = f"`{candidate.key}`"
        choices.append(f"{key}: ignore")
        choices.append(f"{key}: add route only")
        choices.extend(f"{key}: add as backup to `{lane}`" for lane in candidate.lanes)
        if candidate.discovery and candidate.discovery.get("retryable") is True:
            choices.append(f"{key}: test again")
    for anomaly in report.anomalies:
        if anomaly.verdict == PAID_ROUTE_VERDICT:
            choices.append(f"`{anomaly.route_id}`: remove route")
            choices.append(f"`{anomaly.route_id}`: keep as a paid route")
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
        return "decide under **Decisions**: remove the route, or keep it as a paid route"
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


def _facts_line(candidate) -> str:
    facts = candidate.facts or {}
    context = f"{candidate.context_limit:,} context" if candidate.context_limit else None
    parts = [
        facts.get("params") or facts.get("size"),
        context,
        f"released {facts['released']}" if facts.get("released") else None,
        f"inputs: {facts['inputs']}" if facts.get("inputs") else None,
        f"license {facts['license']}" if facts.get("license") else None,
        "AA " + _score_cell(candidate.score, candidate.below_floor),
    ]
    line = " · ".join(p for p in parts if p)
    if facts.get("expires"):
        line += f" · **free tier ends {facts['expires']}**"
    return line


def _render_decision_block(
    choices: Sequence[str], checked: set[str], candidates=(), anomalies=()
) -> str:
    """Checkboxes grouped by model; the rows are exactly the choices, only prose is added.

    Only the `- [ ] choice` lines are machine-read (checked_decisions/parse_decisions), so any
    heading or description between them is inert.
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
        out += ["", f"### `{lead.model.removesuffix(':free')}`", ""]
        summary = (lead.facts or {}).get("summary") or next(
            ((c.facts or {}).get("summary") for c in members if (c.facts or {}).get("summary")),
            "",
        )
        if summary:
            out += [f"> {_inline(summary)}", ""]
        out += [_facts_line(lead), ""]
        for candidate in members:
            if not rows[candidate.key]:
                continue
            method = candidate.structured_output_method or "unverified"
            label = f"\n**{candidate.provider}** · JSON {method} · proven {candidate.proven_on}"
            if candidate.other_lanes:
                label += " · could also fit: " + ", ".join(
                    f"{lane} (-{gap:g})" for lane, gap in candidate.other_lanes
                )
            out.append(label)
            out += [row(x) for x in rows[candidate.key]]
            placed.update(rows[candidate.key])
        links = [
            f"[{label}]({url})"
            for label, url in dict.fromkeys(link for c in members for link in c.links)
        ]
        card = (lead.facts or {}).get("card")
        if card:
            links.insert(0, f"[model card]({card})")
        if links:
            out += ["", "Research: " + " · ".join(links)]
    leftover = [x for x in choices if x not in placed]
    if leftover:
        by_route = {a.route_id: a for a in anomalies}
        out += ["", "### Route and limit decisions"]
        current = None
        for choice in leftover:
            match = re.match(r"`([^`]+)`: ", choice)
            route = by_route.get(match.group(1)) if match else None
            if route is not None and route.route_id != current:
                current = route.route_id
                out += ["", f"**`{route.route_id}`** ({route.verdict}): {_inline(route.reason)}"]
            elif route is None and current is not None:
                current = None
                out.append("")
            out.append(row(choice))
    out.append(DECISION_BLOCK_END)
    return "\n".join(out)


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
    quality_note = (
        f"**Quality data unavailable: {_inline(report.quality_error)}.** Every model shows as "
        "unscored until this is fixed."
        if report.quality_error
        else f"floor (lower of GPT-OSS-120B and Nemotron-3 Super) = {floor}."
    )
    lines = [
        MARKER,
        f"Weekly provider-catalog reconciliation, {run_date} (review/48). Quality data: "
        f"[Artificial Analysis](https://artificialanalysis.ai/) Intelligence Index; "
        f"{quality_note}",
        "",
        f"## Candidates ({len(report.candidates)})",
        "",
    ]
    if report.candidates:
        lines += [
            "Free-marked models that completed a canary on our account and are not configured. "
            "Each model has its own section under **Decisions**, with what is known about it.",
            "",
            "| Model | AA index | Context | JSON method | Proven |",
            "|---|---|---|---|---|",
        ]
        for c in report.candidates:
            context = f"{c.context_limit:,}" if c.context_limit else "unlisted"
            lines.append(
                f"| `{c.key}` | {_score_cell(c.score, c.below_floor)} | {context} | "
                f"{c.structured_output_method or 'unverified'} | {c.proven_on} |"
            )
        lines += [
            "",
            "A lane checkbox is offered when the candidate scores at least that lane's weakest "
            "scored model; other eligible lanes are noted with the gap (capacity backups are "
            "often weaker by design). Promoting a lane's primary model stays a manual edit.",
        ]
    else:
        lines.append("None this week.")
    discoveries = [c for c in report.candidates if c.discovery]
    if discoveries:
        lines += ["", "## Request-cap discovery", ""]
        for candidate in discoveries:
            result = candidate.discovery or {}
            lines += [f"### `{candidate.key}`", ""]
            hints = result.get("litellm")
            if isinstance(hints, dict):
                lines += [
                    f"LiteLLM {hints.get('version', 'unknown')} exact route "
                    f"`{hints.get('key', '')}`: "
                    f"input {_discovery_count(hints.get('input'))}, "
                    f"output {_discovery_count(hints.get('output'))}. "
                    "[Catalog source](https://github.com/BerriAI/litellm/blob/main/"
                    "model_prices_and_context_window.json). "
                    "Starting hints only; missing entries use normal defaults. "
                    "These values are not measured bounds.",
                    "",
                ]
            primary = result.get("input_characterization")
            if isinstance(primary, dict) and primary.get("admitted_input"):
                input_axis = next(
                    (
                        row
                        for row in result.get("axes", [])
                        if isinstance(row, dict) and row.get("axis") == "input"
                    ),
                    {},
                )
                admitted = primary.get("admitted_input")
                generated = primary.get("generated_output")
                claim = input_axis.get("published")
                if (
                    type(admitted) is int
                    and admitted > 0
                    and type(generated) is int
                    and generated >= 0
                    and primary.get("count_basis") == input_axis.get("count_basis")
                    and primary.get("control") == input_axis.get("control")
                ):
                    claim_value = claim if type(claim) is int and claim > 0 else 0
                    operational_window = max(claim_value, admitted + generated)
                    claim_label = _discovery_count(claim) if type(claim) is int else "not published"
                    output_claim = next(
                        (
                            row.get("published")
                            for row in result.get("axes", [])
                            if isinstance(row, dict) and row.get("axis") == "output"
                        ),
                        None,
                    )
                    if not output_claim and isinstance(hints, dict):
                        output_claim = hints.get("output")
                    output_label = (
                        _discovery_count(output_claim)
                        if type(output_claim) is int and output_claim > 0
                        else "not published"
                    )
                    lines += [
                        "**Proposed route values if you select add:** "
                        f"`hard_input_ceiling` = {_discovery_count(admitted)} provider-counted "
                        "input (verified admitted value); "
                        f"`input_context_limit` = {_discovery_count(operational_window)} "
                        f"(max of provider claim {claim_label} and the admitted input plus "
                        f"{_discovery_count(generated)} actually generated output).",
                        "The provider claim and source are preserved in route YAML comments. "
                        "Output/combined probe results do not set caps; the route's separate "
                        "`output_context_limit` comes from a provider/catalog or LiteLLM claim "
                        "and stays visible for review. If neither exists, `/apply` defers for "
                        "a maintainer-entered value.",
                        f"Proposed `output_context_limit`: {output_label}.",
                        "This proposal uses one successful authenticated discovery run; the "
                        "addition remains subject to maintainer review.",
                        "",
                    ]
                lines += [
                    "**Primary input characterization:** confirmed "
                    f"{_discovery_count(primary.get('admitted_input'))} provider-counted input "
                    f"tokens, with {_discovery_count(primary.get('requested_output'))} output "
                    f"requested and {_discovery_count(primary.get('generated_output'))} generated.",
                    f"Passing request local input estimate: "
                    f"{_discovery_count(primary.get('admitted_local_estimate'))} tokens.",
                ]
                rejections = primary.get("rejected_estimates")
                for rejected in rejections[:3] if isinstance(rejections, list) else ():
                    if not isinstance(rejected, dict):
                        continue
                    lines.append(
                        "Rejected input-size request: local estimate "
                        f"{_discovery_count(rejected.get('local_input_estimate'))}; "
                        "approximately "
                        f"{_discovery_count(rejected.get('provider_scale_estimate'))} input tokens "
                        "after calibration against the passing request. "
                        "This is an estimated rejected size, not a provider-counted upper bound."
                    )
                lines.append("")
            if result.get("profile") == "input_priority":
                lines += ["Output and combined checks are supporting examples, not maxima.", ""]
                examples = {
                    row["axis"]: row
                    for row in _discovery_attempt_diagnostics(result.get("attempt_diagnostics"))
                    if row["axis"] in {"output", "total_input_heavy"}
                }
                for axis, example in examples.items():
                    reported_input = example.get("reported_input")
                    generated = example.get("reported_output")
                    consumed = (
                        reported_input + generated
                        if isinstance(reported_input, int) and isinstance(generated, int)
                        else None
                    )
                    lines.append(
                        f"Supporting `{axis}` example: reported input "
                        f"{_discovery_count(reported_input)}, generated output "
                        f"{_discovery_count(generated)}, requested output "
                        f"{_discovery_count(example.get('requested_output'))}; observed "
                        f"input plus generated output {_discovery_count(consumed)}; "
                        f"finish `{example.get('finish_reason_state', 'unknown')}`. "
                        "Consumed totals are separate from requested-output capacity proof."
                    )
                lines.append("")
            lines += [
                "| Axis | Catalog claim | Admitted | Refused | Width / goal | "
                "Count basis / control | Status / note |",
                "|---|---:|---:|---:|---:|---|---|",
            ]
            for axis in result.get("axes", []):
                claim = axis.get("published")
                admitted, refused, width = (
                    axis.get("admitted"),
                    axis.get("refused"),
                    axis.get("target_width"),
                )
                source = axis.get("source")
                claim_text = _discovery_count(claim)
                if source:
                    claim_text += f" ([source]({source}))"
                basis = axis.get("count_basis") or "unverified"
                control = axis.get("control") or "—"
                note = axis.get("reason") or "—"
                lines.append(
                    f"| {axis.get('axis', 'unknown')} | {claim_text} | "
                    f"{_discovery_count(admitted)} | {_discovery_count(refused)} | "
                    f"{_discovery_count(width)} | {basis}; `{control}` | "
                    f"{axis.get('status', 'inconclusive')}; {note} |"
                )
            lines += [
                "",
                "The admitted count is the largest verified passing value to consider as an "
                "operational cap for the tested request conditions; the refused count, "
                "when present, "
                "is a measured upper boundary. An empty refused cell does not mean no approximate "
                "boundary was found. These "
                "results are evidence only and are never written to configuration automatically.",
                f"Reachability: {result.get('reachability', 'verified')}; "
                f"AA index: {_score_cell(candidate.score, candidate.below_floor)}; "
                f"spent: {result.get('calls', 0)} calls, "
                f"{_discovery_count(result.get('input_tokens', 0))} reserved input, "
                f"{_discovery_count(result.get('output_tokens', 0))} requested output tokens.",
                f"Status: {result.get('reason', 'partial')}. "
                f"{result.get('detail', 'Only verified comparable counts establish bounds.')}",
            ]
            attempts = _discovery_attempt_diagnostics(result.get("attempt_diagnostics"))
            if attempts:
                lines += [
                    "",
                    "Recent request diagnostics (response text is shown only as a redacted, "
                    "bounded preview on failed attempts):",
                    "",
                    "| Axis / target | Outcome / diagnostic | HTTP / media / bytes | "
                    "Content / finish / usage | Provider counts | Stream bytes / events | "
                    "Sentinel accuracy | "
                    "Correlation IDs | Preview |",
                    "|---|---|---|---|---:|---|---|---|---|",
                ]
                for attempt in attempts:
                    status = attempt["response_status"] or "—"
                    counts = (
                        f"{_discovery_count(attempt['reported_input'])} / "
                        f"{_discovery_count(attempt['reported_output'])}"
                    )
                    ids = (
                        ", ".join(
                            f"`{attempt[name]}`"
                            for name in ("provider_request_id", "response_id")
                            if attempt[name]
                        )
                        or "—"
                    )
                    lines.append(
                        f"| {attempt['axis']} / {_discovery_count(attempt['target'])} | "
                        f"{attempt['outcome']} / `{attempt['diagnostic']}` | "
                        f"{status} / {attempt['response_media_type']} / "
                        f"{_discovery_count(attempt['response_bytes'])} | "
                        f"{attempt['message_content_state']} / "
                        f"{attempt['finish_reason_state']} ({attempt['finish_reason']}) / "
                        f"{attempt['usage_state']} | {counts} | "
                        f"{_discovery_count(attempt['stream_bytes'])} / "
                        f"{_discovery_count(attempt['stream_events'])} | "
                        f"{attempt['sentinel_verification']} | {ids} | "
                        f"{attempt['response_preview'] or '—'} |"
                    )
                lines.append(
                    "Input admission measures API capacity using provider usage. Sentinel accuracy "
                    "is a separate retrieval/truncation warning, not a token-size refusal."
                )
            for group in _discovery_transport_groups(result.get("transport_retries")):
                lines.append("No-response attempt telemetry:")
                for attempt in group:
                    lines.append(
                        "- "
                        f"{attempt.get('axis', 'unknown')} target "
                        f"{attempt['target']}: "
                        f"{attempt['reserved_input']} reserved input, "
                        f"{attempt['requested_output']} requested output; "
                        f"{attempt['outcome']} / {attempt['diagnostic']} "
                        f"(attempt `{attempt['attempt_id']}`)"
                    )
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
                f"- {flag}`{a.route_id}` (`{a.model}`): **{a.verdict}** -- "
                f"{_inline(a.reason)}{where}"
            )
            if not a.lane_usage:
                lines.append("  - used by no LLM lane")
            for lane, alternates in a.lane_usage:
                still = (
                    ", ".join(f"`{m}`" for m in alternates) or "**nothing else -- the lane stalls**"
                )
                lines.append(f"  - used by `{lane}`; still served by: {still}")
            lines.append(f"  - **Next:** {_anomaly_next(a)}")
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
                "with those changes (review/48). Ticks are kept across updates. A free route "
                "that became paid is never removed automatically: remove it, or keep it as a "
                "paid route. Retired routes are removed by their own automatic PR after a fresh "
                "confirmation, so they need no tick. `add route only` adds the route with a "
                "provisional input cap and no lane; lane placement needs measured evidence. "
                "Partial request-cap discovery can be resumed with `test again`; that only "
                "queues a later bounded run. Unavailable proofs remain ticked. An open PR does "
                "not fulfill a selection until it is merged.",
                "",
                _render_decision_block(
                    choices,
                    checked,
                    [*report.candidates, *pending],
                    [*report.anomalies, *pending_paid],
                ),
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
        observations = f"\n{summary}\n\nOmitted: the issue body limit was reached.\n\n</details>\n"
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
