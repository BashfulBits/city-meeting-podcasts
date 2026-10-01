"""Read-only LLM backlog trends from the append-only scoped run events (review/50)."""

from __future__ import annotations

import argparse
import json
import tempfile
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
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
_WORKING_CLASSES = ("queued", "ingress_limited", "stopped")
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
    return datetime.fromisoformat(event["ts"]).astimezone(UTC)


def _read_events(paths, *, since: datetime) -> tuple[list[dict], int]:
    events = []
    skipped = 0
    for path in paths:
        try:
            event = json.loads(path.read_text())
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            if _timestamp(event) >= since:
                events.append(event)
        except (OSError, ValueError, TypeError, KeyError):
            skipped += 1
    return events, skipped


def load_events(state_dir: Path, *, since: datetime) -> list[dict]:
    return _read_events(sorted((state_dir / "run_events").glob("*.json")), since=since)[0]


def daily_points(events, verb: str, params, *, through: date | None = None) -> list[DayPoint]:
    spec = VERBS[verb]
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
            owner = STAGE_TOKENS[spec.stage].get(token)
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


def analyze(events, verb: str, params) -> VerbReport:
    points = daily_points(events, verb, params)
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
            if VERBS[verb].throughput_reliable
            else trend == "growing"
        )
        if constrained:
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
    events = list(events)
    return {verb: analyze(events, verb, params) for verb in VERBS}


def params_from_config(site_config: Mapping) -> BacklogParams:
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


def _unclassified_tokens(reports: Mapping[str, VerbReport]) -> dict[str, dict[str, int]]:
    # Sibling verbs observe the same stage snapshot: record each unknown stage token only once.
    result: dict[str, dict[str, int]] = {}
    for verb, report in reports.items():
        if report.unclassified:
            result.setdefault(VERBS[verb].stage, {}).update(report.unclassified)
    return result


def render_markdown(reports: Mapping[str, VerbReport]) -> str:
    lines = [
        "| Verb | Backlog | Trend | Drain days | Constrained | Action | "
        "Blocked/held | Unclassified |",
        "|---|---:|---|---:|---|---|---|---|",
    ]
    for verb, report in reports.items():
        blocked = ", ".join(
            f"{key}: {value}" for key, value in report.blocked_or_held.items() if value
        )
        unknown = ", ".join(f"{key}: {value}" for key, value in report.unclassified.items())
        # Unknown event tokens are untrusted text, including Markdown table delimiters.
        unknown = unknown.replace("|", "\\|").replace("\n", " ").replace("\r", " ")
        drain = f"{report.drain_days:.2f}" if report.drain_days is not None else "—"
        lines.append(
            f"| {verb} | {report.backlog} | {report.trend} | {drain} | "
            f"{str(report.constrained).lower()} | {report.action or '—'} | "
            f"{blocked or '—'} | {unknown or '—'} |"
        )
    return "\n".join(lines) + "\n"


def _now() -> datetime:
    return datetime.now(UTC)


def main(argv: list[str] | None = None) -> int:
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
    params = params_from_config(load_site_config(args.site_config))
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
        from citypods.statesync import STATE_PREFIX, pull_state
        from citypods.storage import make_storage

        site = load_site_config(args.site_config)
        storage = make_storage(site, site.get("base_url", ""), Path(args.output_dir))
        if storage is None:
            parser.error("No storage configured; supply --state-dir for local events")
        prefix = f"{STATE_PREFIX}/run_events/"
        cutoff = since.strftime("%Y%m%dT%H%M%S")
        keys = sorted(
            key
            for key, _ in storage.list_objects(prefix)
            if key.endswith(".json") and Path(key).name[:15] >= cutoff
        )
        with tempfile.TemporaryDirectory(prefix="backlog-trend-") as directory:
            state_dir = Path(directory)
            rels = [key[len(STATE_PREFIX) + 1 :] for key in keys]
            pull_state(storage, state_dir, only_paths=rels)
            events, skipped = _read_events([state_dir / rel for rel in rels], since=since)
    reports = analyze_all(events, params)
    unknown = _unclassified_tokens(reports)
    output = {
        "generated_at": now.isoformat(),
        "window_days": params.window_days,
        "params": asdict(params),
        "verbs": {verb: asdict(report) for verb, report in reports.items()},
        "unclassified_tokens": unknown,
        "skipped_files": skipped,
    }
    markdown = render_markdown(reports)
    if args.json:
        args.json.write_text(json.dumps(output, indent=2) + "\n")
    if args.markdown:
        args.markdown.write_text(markdown)
    if not args.json and not args.markdown:
        print(markdown, end="")
    if not events:
        return 2
    return 1 if args.fail_on_unclassified and unknown else 0


if __name__ == "__main__":
    raise SystemExit(main())
