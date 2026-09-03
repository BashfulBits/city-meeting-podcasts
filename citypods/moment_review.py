"""Small authenticated-review adapter for R6's immutable calibration ledger."""

from __future__ import annotations

import argparse
import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from citypods.compute.llm_lanes import lane_for
from citypods.moment_evaluation import load_state, record_review, save_state, state_lock
from citypods.review_issues import render_decision_block, require_one_decision

_REVIEW_CHOICES = ("Good", "Borderline", "Reject")


def _record_parser(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--label", choices=("Good", "Borderline", "Reject"), required=True)
    parser.add_argument("--reviewer", required=True)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--start", type=float)
    parser.add_argument("--end", type=float)
    parser.add_argument("--title")
    parser.add_argument("--caption")
    parser.add_argument("--crop-anchor", help="JSON object with normalized x/y crop anchor")
    parser.add_argument("--composition", dest="output_profile")
    return None


def record(args: argparse.Namespace) -> int:
    candidate = json.loads(args.candidate.read_text())
    if not isinstance(candidate, dict):
        raise SystemExit("--candidate must contain one JSON candidate object")
    overrides = {
        key: value
        for key, value in {
            "start": args.start,
            "end": args.end,
            "title": args.title,
            "caption": args.caption,
            "output_profile": args.output_profile,
        }.items()
        if value is not None
    }
    if args.crop_anchor:
        overrides["crop_anchor"] = json.loads(args.crop_anchor)
    with state_lock(args.state):
        state = load_state(args.state)
        record_review(
            state,
            candidate,
            args.label,
            reviewer=args.reviewer,
            review_id=args.review_id,
            overrides=overrides,
        )
        save_state(args.state, state)
    return 0


def _review_body(candidate: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(candidate, sort_keys=True).encode()).decode()
    return (
        f"<!-- r6-candidate-b64: {encoded} -->\n"
        f"# R6 moment review: `{candidate['candidate_id']}`\n\n"
        f"> {candidate.get('quote', '')}\n\n"
        f"{render_decision_block(_REVIEW_CHOICES)}\n\n"
        "Optional maintainer controls: `start`, `end`, `title`, `caption`, `crop_anchor`, and "
        "`output_profile` in a fenced `json` block. Caption wording must match the transcript.\n"
    )


def _decision(body: str) -> tuple[dict, str, dict]:
    match = re.search(r"<!-- r6-candidate-b64: ([A-Za-z0-9_=-]+) -->", body)
    if not match:
        raise ValueError("missing trusted R6 candidate payload")
    candidate = json.loads(base64.urlsafe_b64decode(match.group(1)).decode())
    label = require_one_decision(body, _REVIEW_CHOICES)
    controls: dict = {}
    json_matches = list(re.finditer(r"```json\s*(\{.*?\})\s*```", body, flags=re.S))
    json_match = json_matches[-1] if json_matches else None
    if json_match:
        controls = json.loads(json_match.group(1))
        if not isinstance(controls, dict):
            raise ValueError("R6 review controls must be a JSON object")
    return candidate, label, controls


def _validated_ledger_candidate(state_dir: Path, site: dict, candidate: dict) -> dict:
    """Return the durable candidate matching an issue payload, or reject tampering."""
    from citypods.config import load_city_configs
    from citypods.records import load_records, record_to_episode, source_key

    city_slug = str(candidate.get("city_slug") or "")
    candidate_id = str(candidate.get("candidate_id") or "")
    cities = {city.slug: city for city in load_city_configs("config", site.get("defaults", {}))}
    city = cities.get(city_slug)
    if city is None or not candidate_id:
        raise ValueError("R6 review payload is missing its packaged city or candidate identity")
    for raw in load_records(state_dir, source_key(city)).values():
        for row in record_to_episode(raw).moment_pullquote_candidates:
            if isinstance(row, dict) and row.get("candidate_id") == candidate_id:
                for key in (
                    "candidate_id",
                    "quality_score",
                    "meeting_family",
                    "provider_model",
                    "prompt_version",
                    "duration_bucket",
                    "framing_profile",
                    "quote",
                    "start",
                    "end",
                ):
                    if candidate.get(key) != row.get(key):
                        raise ValueError(
                            f"R6 review payload differs from durable candidate field {key}"
                        )
                return row
    raise ValueError("R6 review candidate is no longer present in the durable ledger")


def _dispatch_blocked_reason(state_dir: Path, models: list[object]) -> str | None:
    """Report an all-route capacity pause instead of falsely calling it an empty review set."""
    try:
        ledger = json.loads((state_dir / "llm_budget.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    now = datetime.now(UTC)
    blocked = []
    for value in models:
        model = str(value)
        route = (ledger.get("routes") or {}).get(model) or (ledger.get("routes") or {}).get(
            f"litellm:{model}"
        )
        until = route.get("blocked_until") if isinstance(route, dict) else None
        try:
            if until and datetime.fromisoformat(str(until)).astimezone(UTC) > now:
                blocked.append(str(until))
        except ValueError:
            continue
    if models and len(blocked) == len(models):
        return f"LLM dispatch capacity blocked until {min(blocked)}"
    return None


def package(args: argparse.Namespace) -> int:
    from citypods.config import load_city_configs, load_site_config
    from citypods.records import load_records, record_to_episode, source_key
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state
    from citypods.storage import make_storage

    site = load_site_config(args.site_config)
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site, output_dir)
    storage = make_storage(site, site.get("base_url", ""), output_dir)
    pull_state(storage, state_dir)
    evaluation = (site.get("moments") or {}).get("evaluation") or {}
    state = load_state(state_dir / str(evaluation.get("state_path", "r6_moment_evaluation.json")))
    selected: dict[str, dict] = {}
    for city in load_city_configs(args.config_dir, site.get("defaults", {})):
        for raw in load_records(state_dir, source_key(city)).values():
            episode = record_to_episode(raw)
            for candidate in episode.moment_pullquote_candidates:
                if not isinstance(candidate, dict) or not candidate.get("candidate_id"):
                    continue
                if str(candidate["candidate_id"]) in (state.get("overrides") or {}):
                    continue
                selected[str(candidate["candidate_id"])] = {
                    **candidate,
                    "city_slug": city.slug,
                    "episode_title": episode.title,
                }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = [selected[key] for key in sorted(selected)[: max(0, args.limit)]]
    children = []
    for candidate in rows:
        name = f"{candidate['candidate_id']}.md"
        (out_dir / name).write_text(_review_body(candidate), encoding="utf-8")
        children.append({"candidate_id": candidate["candidate_id"], "body_file": name})
    (out_dir / "parent.md").write_text(
        "# R6 moment calibration review batch\n\n"
        "Review each child issue with exactly one outcome. "
        "Optional JSON controls remain available on each child.\n",
        encoding="utf-8",
    )
    manifest: dict[str, object] = {"version": 1, "children": children}
    if not children:
        # Reads the lane registry rather than the removed `moments.llm_models` key; an empty
        # list here would make _dispatch_blocked_reason report no blocked route at all.
        models = list(lane_for("r6-moments", site).models)
        reason = _dispatch_blocked_reason(state_dir, models)
        if reason:
            manifest["reasons"] = [reason]
    (out_dir / "review-batch.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"packaged {len(children)} R6 review issue(s)")
    return 0


def ingest(args: argparse.Namespace) -> int:
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state, push_state
    from citypods.storage import make_storage

    site = load_site_config(args.site_config)
    output_dir = Path(args.output_dir)
    state_dir = resolve_state_dir(site, output_dir)
    storage = make_storage(site, site.get("base_url", ""), output_dir)
    evaluation = (site.get("moments") or {}).get("evaluation") or {}
    state_path = state_dir / str(evaluation.get("state_path", "r6_moment_evaluation.json"))
    pull_state(storage, state_dir, only_paths=[state_path])
    candidate, label, controls = _decision(Path(args.issue_body_file).read_text(encoding="utf-8"))
    candidate = _validated_ledger_candidate(state_dir, site, candidate)
    with state_lock(state_path):
        state = load_state(state_path)
        record_review(
            state,
            candidate,
            label,
            reviewer=args.actor,
            review_id=f"github-issue-{args.issue_number}",
            overrides=controls,
        )
        save_state(state_path, state)
    push_state(storage, state_dir, only_paths=[state_path])
    print(json.dumps({"stored": True, "candidate_id": candidate["candidate_id"], "label": label}))
    return 0


def main(argv: list[str] | None = None) -> int:
    values = list(argv or [])
    if values and values[0].startswith("--"):
        values.insert(0, "record")
    parser = argparse.ArgumentParser(prog="citypods r6-review")
    sub = parser.add_subparsers(dest="command", required=True)
    record_parser = sub.add_parser("record", help="record one locally authenticated decision")
    _record_parser(record_parser)
    package_parser = sub.add_parser("package", help="package GitHub review issues")
    package_parser.add_argument("--site-config", default="config/site_config.yml")
    package_parser.add_argument("--config-dir", default="config")
    package_parser.add_argument("--output-dir", default="docs")
    package_parser.add_argument("--out-dir", required=True)
    package_parser.add_argument("--limit", type=int, default=80)
    ingest_parser = sub.add_parser("ingest", help="ingest a trusted GitHub review issue")
    ingest_parser.add_argument("--site-config", default="config/site_config.yml")
    ingest_parser.add_argument("--output-dir", default="docs")
    ingest_parser.add_argument("--issue-number", required=True, type=int)
    ingest_parser.add_argument("--issue-body-file", required=True)
    ingest_parser.add_argument("--actor", required=True)
    args = parser.parse_args(values)
    if args.command == "record":
        return record(args)
    if args.command == "package":
        return package(args)
    return ingest(args)


__all__ = ["main"]
