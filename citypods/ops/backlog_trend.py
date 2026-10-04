"""Read-only LLM backlog trends from the append-only scoped run events (review/50)."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import mean


@dataclass(frozen=True)
class BacklogParams:
    window_days: int = 7
    min_points: int = 4
    drain_days_max: float = 14.0
    deadband_floor: float = 1.0
    deadband_rel: float = 0.02


@dataclass(frozen=True)
class VerbSpec:
    name: str
    lane: str
    stage: str
    throughput_reliable: bool
    purpose: str | None = None


VERBS = {
    spec.name: spec
    for spec in (
        VerbSpec("chapter-agenda", "chapter-agenda", "chapter_agenda", True),
        VerbSpec("chapter-locator", "chapter-locator", "chapter_locator", True),
        VerbSpec("tagger", "tag", "tags", False),
        VerbSpec("prelabeler", "tag", "tags", False),
        VerbSpec("prelabeler-shadow", "tag", "tags", False),
        VerbSpec("moments", "moments", "moments", False),
    )
}
STAGE_TOKENS: dict[str, dict[str, tuple[str, str]]] = {
    "chapter_agenda": {
        "llm-pending": ("chapter-agenda", "queued"),
        "producer-cap": ("chapter-agenda", "ingress_limited"),
        "stop-signal": ("chapter-agenda", "stopped"),
        "missing-agenda-artifact": ("chapter-agenda", "blocked"),
        "missing-agenda-artifact-despite-accepted-quality": ("chapter-agenda", "blocked"),
        "agenda-artifact-key-present-but-unreadable": ("chapter-agenda", "blocked"),
    },
    "chapter_locator": {
        "llm-pending": ("chapter-locator", "queued"),
        "producer-cap": ("chapter-locator", "ingress_limited"),
        "stop-signal": ("chapter-locator", "stopped"),
        "agenda-not-complete": ("chapter-locator", "blocked"),
        "missing-timed-transcript": ("chapter-locator", "blocked"),
    },
    "tags": {
        "tag-llm-dispatch": ("tagger", "queued"),
        "tag-llm-no-quota": ("tagger", "ingress_limited"),
        "tag-llm-stop": ("tagger", "stopped"),
        "tag-budget-stop": ("tagger", "stopped"),
        "tag-llm-oversized": ("tagger", "blocked"),
        "tag-prelabeler-dispatch": ("prelabeler", "queued"),
        "tag-prelabeler-no-quota": ("prelabeler", "ingress_limited"),
        "tag-prelabeler-stop": ("prelabeler", "stopped"),
        "tag-prelabeler-oversized": ("prelabeler", "blocked"),
        "tag-prelabeler-shadow-dispatch": ("prelabeler-shadow", "queued"),
        "tag-prelabeler-shadow-no-quota": ("prelabeler-shadow", "ingress_limited"),
        "tag-prelabeler-shadow-error": ("prelabeler-shadow", "errored"),
    },
    "moments": {
        "llm-pending": ("moments", "queued"),
        "llm-capacity": ("moments", "ingress_limited"),
        "stop": ("moments", "stopped"),
        "rollout-dispatch-cap": ("moments", "policy_held"),
        "llm-error": ("moments", "errored"),
    },
}
_WORKING_CLASSES = ("ready", "queued", "ingress_limited", "stopped")
_OTHER_CLASSES = ("blocked", "policy_held", "errored")


@dataclass(frozen=True)
class DayPoint:
    day: date
    backlog: int
    ran_day: int
    classes: dict[str, int]
    unclassified: dict[str, int]


@dataclass(frozen=True)
class VerbReport:
    days: int
    backlog: int
    classes: dict[str, int]
    blocked_or_held: dict[str, int]
    slope: float | None
    mean: float | None
    trend: str
    throughput_per_day: float | None
    drain_days: float | None
    constrained: bool
    action: str | None
    unclassified: dict[str, int]


def _timestamp(event: dict) -> datetime:
    """Parse the event timestamp as UTC for daily grouping and cutoff comparisons."""
    return datetime.fromisoformat(event["ts"]).astimezone(UTC)


def _read_events(paths, *, since: datetime) -> tuple[list[dict], int]:
    """Read recent JSON event objects and count unreadable or malformed files."""
    events = []
    skipped = 0
    for path in paths:
        try:
            event = json.loads(path.read_text())
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            work = event.get("llm_work")
            if work is not None:
                if not isinstance(work, dict) or work.get("schema_version") != 1:
                    raise ValueError("unknown LLM work schema")
                purposes = work.get("purposes")
                if not isinstance(purposes, dict):
                    raise ValueError("invalid LLM purposes")
                for row in purposes.values():
                    if (
                        not isinstance(row, dict)
                        or row.get("coverage") not in {"complete", "partial"}
                        or row.get("scope") not in {"retained_catalog", "sample"}
                        or not isinstance(row.get("unit"), str)
                        or not isinstance(row.get("states"), dict)
                        or any(
                            type(value) is not int or value < 0 for value in row["states"].values()
                        )
                        or type(row.get("consumed")) is not int
                        or row["consumed"] < 0
                        or type(row.get("observed")) is not int
                        or row["observed"] < 0
                    ):
                        raise ValueError("invalid LLM purpose snapshot")
            if event.get("shard"):
                index, count = map(int, event["shard"].split("/"))
                if count <= 0 or not 0 <= index < count:
                    raise ValueError("invalid shard scope")
            if _timestamp(event) >= since:
                events.append(event)
        except (OSError, ValueError, TypeError, KeyError):
            skipped += 1
    return events, skipped


def load_events(state_dir: Path, *, since: datetime) -> list[dict]:
    """Load recent local run events without modifying the state directory."""
    return _read_events(sorted((state_dir / "run_events").glob("*.json")), since=since)[0]


def daily_points(
    events, verb: str, params, *, through: date | None = None, specs=None, tokens=None
) -> list[DayPoint]:
    """Select each day's last completed snapshot and sum all completed daily runs."""
    spec = (VERBS if specs is None else specs)[verb]
    tokens = STAGE_TOKENS if tokens is None else tokens
    purpose = spec.purpose or _PURPOSES.get(verb, verb)
    explicit = [
        event for event in events if purpose in (event.get("llm_work") or {}).get("purposes", {})
    ]
    if explicit:
        return purpose_points(explicit, purpose, params, through=through)
    grouped = defaultdict(list)
    for event in events:
        if (
            event.get("lane") == spec.lane
            and spec.stage in event.get("stages", {})
            and event.get("outcome") != "interrupted"
        ):
            ts = _timestamp(event)
            if through is None or ts.date() <= through:
                grouped[ts.date()].append((ts, event["stages"][spec.stage]))
    points = []
    for day in sorted(grouped)[-params.window_days :]:
        runs = grouped[day]
        latest = max(runs, key=lambda row: row[0])[1]
        classes = dict.fromkeys((*_WORKING_CLASSES, *_OTHER_CLASSES), 0)
        unclassified = {}
        for token, count in (latest.get("defer_reasons") or {}).items():
            owner = tokens.get((spec.lane, spec.stage), tokens.get(spec.stage, {})).get(token)
            if owner is None:
                unclassified[token] = count
            elif owner[0] == verb:
                classes[owner[1]] += count
        points.append(
            DayPoint(
                day=day,
                backlog=sum(classes[key] for key in _WORKING_CLASSES),
                ran_day=sum(run.get("ran", 0) for _, run in runs),
                classes=classes,
                unclassified=unclassified,
            )
        )
    return points


def purpose_points(events, purpose, params, *, through=None):
    """Combine disjoint shard censuses only when every shard has a complete snapshot."""
    if not events:
        return []
    grouped = defaultdict(list)
    latest_event = max(events, key=_timestamp)
    unit = latest_event["llm_work"]["purposes"][purpose]["unit"]
    for event in events:
        row = event["llm_work"]["purposes"][purpose]
        if (
            row.get("unit") != unit
            or row.get("coverage") != "complete"
            or row.get("scope") != "retained_catalog"
            or event.get("interrupted")
            or event.get("outcome") == "interrupted"
            or event.get("source")
            or event.get("city")
        ):
            continue
        ts = _timestamp(event)
        if through is None or ts.date() <= through:
            grouped[ts.date()].append((ts, event.get("shard"), row))
    points = []
    for day in sorted(grouped)[-params.window_days :]:
        runs = grouped[day]
        # Prefer a full census to overlapping shard observations. Sum throughput only from
        # the same scope, so a diagnostic whole-catalog replay cannot double-count shard runs.
        full = [entry for entry in runs if not entry[1]]
        if full:
            chosen = [max(full, key=lambda entry: entry[0])]
            daily_runs = full
        else:
            latest = {}
            for entry in sorted(runs, key=lambda entry: entry[0]):
                latest[entry[1]] = entry
            counts = {int(shard.split("/")[1]) for shard in latest}
            if len(counts) != 1:
                continue
            count = counts.pop()
            if {int(shard.split("/")[0]) for shard in latest} != set(range(count)):
                continue
            chosen = list(latest.values())
            daily_runs = runs
        classes = dict.fromkeys((*_WORKING_CLASSES, *_OTHER_CLASSES), 0)
        unknown = defaultdict(int)
        for _, _, row in chosen:
            states = row.get("states") or {}
            for key in classes:
                classes[key] += states.get(key, 0)
            for key, value in states.items():
                if key not in {"ready", "consumed", "reused", *classes}:
                    unknown[key] += value
        points.append(
            DayPoint(
                day,
                sum(classes[key] for key in _WORKING_CLASSES),
                sum(row.get("consumed", 0) for _, _, row in daily_runs),
                classes,
                dict(unknown),
            )
        )
    return points


def analyze(events, verb: str, params, *, specs=None, tokens=None) -> VerbReport:
    """Measure one verb's trend and recommend an action when working backlog remains."""
    specs = VERBS if specs is None else specs
    points = daily_points(events, verb, params, specs=specs, tokens=tokens)
    latest = points[-1] if points else None
    classes = latest.classes if latest else dict.fromkeys((*_WORKING_CLASSES, *_OTHER_CLASSES), 0)
    backlog = latest.backlog if latest else 0
    slope = average = throughput = drain = None
    trend = "insufficient_data"
    constrained = False
    action = None
    if len(points) >= params.min_points:
        average = mean(point.backlog for point in points)
        center = (len(points) - 1) / 2
        denominator = sum((index - center) ** 2 for index in range(len(points)))
        slope = (
            sum((index - center) * (point.backlog - average) for index, point in enumerate(points))
            / denominator
            if denominator
            else 0.0
        )
        threshold = max(params.deadband_floor, params.deadband_rel * average)
        trend = "growing" if slope >= threshold else "shrinking" if slope <= -threshold else "flat"
        throughput = mean(point.ran_day for point in points)
        drain = backlog / throughput if throughput else None
        constrained = (
            backlog > 0
            and (drain is None or drain > params.drain_days_max)
            and trend != "shrinking"
            if specs[verb].throughput_reliable
            else trend == "growing"
        )
        constrained = constrained and classes.get("ready", 0) == 0
        if constrained and backlog > 0:
            action = (
                "raise_ingress_quota"
                if classes["ingress_limited"] / backlog >= 0.5
                else "add_route_capacity"
            )
    # Match review/50's report precision; all judgements above use the unrounded measurements.
    return VerbReport(
        days=len(points),
        backlog=backlog,
        classes={key: classes[key] for key in _WORKING_CLASSES},
        blocked_or_held={key: classes[key] for key in _OTHER_CLASSES},
        slope=round(slope, 2) if slope is not None else None,
        mean=round(average, 1) if average is not None else None,
        trend=trend,
        throughput_per_day=round(throughput, 1) if throughput is not None else None,
        drain_days=round(drain, 2) if drain is not None else None,
        constrained=constrained,
        action=action,
        unclassified=latest.unclassified if latest else {},
    )


def analyze_all(events, params) -> dict[str, VerbReport]:
    """Analyze all six verbs, materializing iterators once for shared stage consumers."""
    events = list(events)
    return {verb: analyze(events, verb, params) for verb in VERBS}


# Existing shared-stage ownership remains explicit: run events do not contain purposes.
_PURPOSES = {
    "tagger": "topic-tags:tagger",
    "prelabeler": "topic-tags:prelabeler",
    "prelabeler-shadow": "topic-tags:prelabeler-shadow",
    "moments": "r6-moments",
}
_GENERIC_CLASSES = {
    "llm-pending": "queued",
    "producer-cap": "ingress_limited",
    "llm-capacity": "ingress_limited",
    "judge-capacity": "ingress_limited",
    "stop": "stopped",
    "stop-signal": "stopped",
    "rollout-dispatch-cap": "policy_held",
    "llm-error": "errored",
}


def discover_verbs(events, site_config):
    """Discover registered purposes and LLM stage telemetry without guessing unknown tokens.

    A registry-only purpose has no telemetry, rather than a measured zero backlog. Historical
    stage rows remain visible while their events are in the read window. Shared-stage purposes
    require producer telemetry to distinguish them; unmatched purposes stay visibly unmeasured.
    """
    registry = site_config.get("llm_lanes")
    specs = dict(VERBS)
    tokens = {stage: dict(rows) for stage, rows in STAGE_TOKENS.items()}
    lifecycle = {}
    for verb in specs:
        purpose = _PURPOSES.get(verb, verb)
        lifecycle[verb] = "active" if registry is None or purpose in registry else "retired"
    registered = set(registry or {})
    mapped = {_PURPOSES.get(verb, verb) for verb in specs}
    for purpose in sorted(registered - mapped):
        specs[purpose] = VerbSpec(purpose, purpose, purpose.replace("-", "_"), False)
        lifecycle[purpose] = "no_telemetry"
    candidates = defaultdict(set)
    for event in events:
        if event.get("outcome") == "interrupted":
            continue
        lane = event.get("lane")
        if not isinstance(lane, str):
            continue
        for stage, stats in event.get("stages", {}).items():
            if stage in STAGE_TOKENS or not isinstance(stats, dict):
                continue
            reasons = stats.get("defer_reasons") or {}
            matches = [
                purpose
                for purpose in registered
                if purpose.replace("-", "_") == stage.replace("-", "_") or purpose == lane
            ]
            if not matches and not any(
                token in {"llm-pending", "llm-capacity", "judge-capacity", "llm-error"}
                for token in reasons
            ):
                continue
            candidates[(lane, stage)].update(matches)
    ownership = defaultdict(set)
    for pair, matches in candidates.items():
        for purpose in matches:
            ownership[purpose].add(pair)
    for (lane, stage), matches in sorted(candidates.items()):
        unique = len(matches) == 1 and len(ownership[next(iter(matches))]) == 1
        verb = next(iter(matches)) if unique else f"{lane}:{stage}"
        specs[verb] = VerbSpec(verb, lane, stage, False)
        lifecycle[verb] = "active" if unique else "unregistered"
        tokens[(lane, stage)] = {
            token: (verb, category) for token, category in _GENERIC_CLASSES.items()
        }
    for verb, spec in specs.items():
        if lifecycle[verb] == "active" and not any(
            event.get("lane") == spec.lane
            and spec.stage in event.get("stages", {})
            and event.get("outcome") != "interrupted"
            for event in events
        ):
            lifecycle[verb] = "no_telemetry"
    explicit = {
        purpose for event in events for purpose in (event.get("llm_work") or {}).get("purposes", {})
    }
    reverse = {purpose: verb for verb, purpose in _PURPOSES.items()}
    for purpose in sorted(explicit):
        verb = reverse.get(purpose, purpose)
        specs[verb] = VerbSpec(verb, purpose, purpose, True, purpose)
        lifecycle[verb] = "active" if registry is None or purpose in registered else "retired"
    return specs, tokens, lifecycle


def params_from_config(site_config: Mapping) -> BacklogParams:
    """Read optional backlog settings, using defaults and rejecting unknown keys."""
    raw = site_config.get("llm_backlog") or {}
    if not isinstance(raw, Mapping):
        raise ValueError("llm_backlog must be a mapping")
    unknown = set(raw) - {field.name for field in fields(BacklogParams)}
    if unknown:
        raise ValueError(f"Unknown llm_backlog keys: {', '.join(sorted(unknown))}")
    params = BacklogParams(**raw)
    if params.window_days < 1 or params.min_points < 1:
        raise ValueError("window_days and min_points must be positive")
    return params


def _unclassified_tokens(
    reports: Mapping[str, VerbReport], specs=None
) -> dict[str, dict[str, int]]:
    # Sibling verbs observe the same stage snapshot: record each unknown stage token only once.
    """Collect unknown tokens once per stage rather than once per sibling verb."""
    specs = VERBS if specs is None else specs
    result: dict[str, dict[str, int]] = {}
    for verb, report in reports.items():
        if report.unclassified:
            result.setdefault(specs[verb].stage, {}).update(report.unclassified)
    return result


def render_markdown(reports: Mapping[str, VerbReport], lifecycle=None, *, unmeasured=()) -> str:
    """Render advisory backlog measurements as a Markdown summary table."""
    lines = [
        "| Verb | Backlog | Trend | Drain days | Constrained | Action | "
        "Blocked/held | Unclassified | Lifecycle |",
        "|---|---:|---|---:|---|---|---|---|---|",
    ]
    for verb, report in reports.items():
        blocked = ", ".join(
            f"{key}: {value}" for key, value in report.blocked_or_held.items() if value
        )
        unknown = ", ".join(f"{key}: {value}" for key, value in report.unclassified.items())
        # Unknown event tokens are untrusted text, including Markdown table delimiters.
        unknown = unknown.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        drain = f"{report.drain_days:.2f}" if report.drain_days is not None else "—"
        label = verb.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        status = (lifecycle or {}).get(verb, "active")
        backlog = "—" if status == "no_telemetry" or verb in unmeasured else str(report.backlog)
        lines.append(
            f"| {label} | {backlog} | {report.trend} | {drain} | "
            f"{str(report.constrained).lower()} | {report.action or '—'} | "
            f"{blocked or '—'} | {unknown or '—'} | {status} |"
        )
    return "\n".join(lines) + "\n"


def _now() -> datetime:
    """Return the current UTC timestamp for report generation and event selection."""
    return datetime.now(UTC)


def main(argv: list[str] | None = None) -> int:
    """Read local or selected remote events, write reports, and return the report exit code."""
    from citypods.config import load_site_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-config", default="config/site_config.yml")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--window-days", type=int)
    parser.add_argument("--json", type=Path)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--fail-on-unclassified", action="store_true")
    args = parser.parse_args(argv)
    site = load_site_config(args.site_config)
    params = params_from_config(site)
    if args.window_days is not None:
        params = params_from_config(
            {"llm_backlog": {**asdict(params), "window_days": args.window_days}}
        )
    now = _now()
    since = now - timedelta(days=params.window_days + 2)
    if args.state_dir is not None:
        paths = sorted((args.state_dir / "run_events").glob("*.json"))
        events, skipped = _read_events(paths, since=since)
    else:
        from citypods.statesync import STATE_PREFIX
        from citypods.storage import make_storage
        from citypods.storage.s3 import is_transient_storage_error

        site = load_site_config(args.site_config)
        storage = make_storage(site, site.get("base_url", ""), Path(args.output_dir))
        if storage is None:
            parser.error("No storage configured; supply --state-dir for local events")
        prefix = f"{STATE_PREFIX}/run_events/"
        cutoff = since.strftime("%Y%m%dT%H%M%S")
        keys = sorted(
            key
            for key, _ in storage.list_objects(prefix)
            if key.startswith(prefix) and key.endswith(".json") and Path(key).name[:15] >= cutoff
        )
        with tempfile.TemporaryDirectory(prefix="backlog-trend-") as directory:
            # Append-only run events are not snapshot-manifest members. Read the listed keys
            # directly, using local numeric names so remote keys cannot escape the temp directory.
            paths = [Path(directory) / f"{index:06d}.json" for index in range(len(keys))]

            def download(pair: tuple[str, Path]) -> None:
                key, path = pair
                try:
                    storage.get_file(key, path)
                except Exception as exc:
                    if not is_transient_storage_error(exc):
                        raise
                    # Missing or transiently unreadable files are counted by _read_events.
                    path.unlink(missing_ok=True)

            if keys:
                with ThreadPoolExecutor(max_workers=min(8, len(keys))) as pool:
                    list(pool.map(download, zip(keys, paths, strict=True)))
            events, skipped = _read_events(paths, since=since)
    specs, tokens, lifecycle = discover_verbs(events, site)
    reports = {verb: analyze(events, verb, params, specs=specs, tokens=tokens) for verb in specs}
    # Unknown ownership/classification cannot support a capacity recommendation.
    reports = {
        verb: replace(report, constrained=False, action=None)
        if report.unclassified or lifecycle[verb] != "active"
        else report
        for verb, report in reports.items()
    }
    unknown = _unclassified_tokens(reports, specs)
    output = {
        "generated_at": now.isoformat(),
        "window_days": params.window_days,
        "params": asdict(params),
        "verbs": {verb: asdict(report) for verb, report in reports.items()},
        "unclassified_tokens": unknown,
        "skipped_files": skipped,
        "verb_lifecycle": lifecycle,
    }
    for verb, status in lifecycle.items():
        if status == "no_telemetry":
            output["verbs"][verb]["backlog"] = None
    for verb, spec in specs.items():
        purpose = spec.purpose or _PURPOSES.get(verb, verb)
        rows = [
            (event, (event.get("llm_work") or {}).get("purposes", {}).get(purpose))
            for event in events
        ]
        rows = [(event, row) for event, row in rows if row is not None]
        if rows:
            event, row = max(rows, key=lambda pair: _timestamp(pair[0]))
            output["verbs"][verb].update(
                {
                    "unit": row["unit"],
                    "coverage": row["coverage"],
                    "scope": row["scope"],
                    "telemetry_source": "purpose_snapshot",
                    "observed": row["observed"],
                    "consumed_run": row["consumed"],
                    "states": row["states"],
                    "job_outcomes": row.get("job_outcomes", {}),
                }
            )
            if (
                row["coverage"] != "complete"
                or row["scope"] == "sample"
                or event.get("source")
                or event.get("city")
                or event.get("interrupted")
            ):
                reports[verb] = replace(reports[verb], constrained=False, action=None)
                output["verbs"][verb].update(constrained=False, action=None, backlog=None)
            measured = purpose_points([event for event, _ in rows], purpose, params)
            if not measured or measured[-1].day != _timestamp(event).date():
                reports[verb] = replace(reports[verb], constrained=False, action=None)
                output["verbs"][verb].update(constrained=False, action=None, backlog=None)
    unmeasured = {verb for verb, row in output["verbs"].items() if row["backlog"] is None}
    markdown = render_markdown(reports, lifecycle, unmeasured=unmeasured)
    metadata = [(verb, row) for verb, row in output["verbs"].items() if row.get("telemetry_source")]
    if metadata:
        markdown += "\n| Purpose | Unit | Coverage | Scope | Observed |\n"
        markdown += "|---|---|---|---|---:|\n"
        for verb, row in metadata:
            label = verb.replace("|", "\\|").replace("\n", " ")
            unit_label = row["unit"].replace("|", "\\|").replace("\n", " ")
            markdown += (
                f"| {label} | {unit_label} | {row['coverage']} | "
                f"{row['scope']} | {row['observed']} |\n"
            )
    if args.json:
        args.json.write_text(json.dumps(output, indent=2) + "\n")
    if args.markdown:
        args.markdown.write_text(markdown)
    if not args.json and not args.markdown:
        print(markdown, end="")
    if not events and (skipped or args.fail_on_unclassified):
        return 2
    return 1 if args.fail_on_unclassified and unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
