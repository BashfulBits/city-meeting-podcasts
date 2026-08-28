"""Durable, bounded retained-catalog tag backfill requests for tournament route changes."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

STATE = "llm_tournament_backfills.json"


def _state(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        value = {}
    if not isinstance(value, dict):
        value = {}
    value.setdefault("version", 1)
    value.setdefault("requests", {})
    return value


def _save(storage, state_dir: Path, state: dict) -> None:
    from citypods.statesync import push_state

    path = state_dir / STATE
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    push_state(storage, state_dir, only_paths=[STATE])


def request(*, site_config_path: str, output_dir: str, issue_number: int, model: str) -> int:
    """Persist one idempotent post-merge backfill request."""
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state
    from citypods.storage import make_storage

    site = load_site_config(site_config_path)
    state_dir = resolve_state_dir(site, Path(output_dir))
    storage = make_storage(site, site.get("base_url", ""), Path(output_dir))
    pull_state(storage, state_dir)
    state = _state(state_dir / STATE)
    request_id = f"tag:{issue_number}:{model}"
    row = state["requests"].setdefault(
        request_id,
        {
            "task": "tag",
            "issue_number": issue_number,
            "model": model,
            "status": "pending",
            "cursor": 0,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    _save(storage, state_dir, state)
    print(json.dumps({"request_id": request_id, "status": row["status"]}, sort_keys=True))
    return 0


def next_source(
    *, site_config_path: str, output_dir: str, issue_number: int | None, model: str | None
) -> int:
    """Advance a resumable source cursor and print the next source needing the selected route."""
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state
    from citypods.storage import make_storage

    site = load_site_config(site_config_path)
    state_dir = resolve_state_dir(site, Path(output_dir))
    storage = make_storage(site, site.get("base_url", ""), Path(output_dir))
    pull_state(storage, state_dir)
    state = _state(state_dir / STATE)
    if issue_number is None or model is None:
        active = [
            (key, value)
            for key, value in state["requests"].items()
            if isinstance(value, dict) and value.get("status") in {"pending", "active"}
        ]
        if not active:
            print(json.dumps({"status": "no_request"}, sort_keys=True))
            return 0
        request_id, request = sorted(active)[0]
        issue_number = int(request["issue_number"])
        model = str(request["model"])
    else:
        request_id = f"tag:{issue_number}:{model}"
        request = state["requests"].get(request_id)
    if not isinstance(request, dict):
        raise ValueError("tournament backfill request was not found")
    target = f"litellm:{model}"
    sources = []
    for path in sorted(state_dir.glob("sources/*/episodes.json")):
        try:
            episodes = json.loads(path.read_text(encoding="utf-8")).get("episodes") or {}
        except (OSError, ValueError):
            continue
        needs_route = False
        for record in episodes.values():
            candidates = record.get("llm_tag_candidates") if isinstance(record, dict) else None
            model_values = {
                str(candidate.get("provider_model") or "")
                for candidate in candidates or []
                if isinstance(candidate, dict) and candidate.get("source_kind", "llm") != "rule"
            }
            if model_values and target not in model_values:
                needs_route = True
                break
        if needs_route:
            sources.append(path.parent.name)
    if not sources:
        request["status"] = "completed"
        request["completed_at"] = datetime.now(UTC).isoformat()
        _save(storage, state_dir, state)
        print(
            json.dumps(
                {"status": "completed", "issue_number": issue_number, "model": model},
                sort_keys=True,
            )
        )
        return 0
    cursor = int(request.get("cursor") or 0) % len(sources)
    source = sources[cursor]
    request["status"] = "active"
    request["cursor"] = (cursor + 1) % len(sources)
    request["last_source"] = source
    request["updated_at"] = datetime.now(UTC).isoformat()
    _save(storage, state_dir, state)
    print(
        json.dumps(
            {"status": "active", "source": source, "issue_number": issue_number, "model": model},
            sort_keys=True,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citypods tournament-backfill")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("request", "next-source"):
        item = sub.add_parser(command)
        item.add_argument("--site-config", default="config/site_config.yml")
        item.add_argument("--output-dir", default="docs")
        item.add_argument("--issue-number", type=int)
        item.add_argument("--model")
    args = parser.parse_args(argv)
    if args.command == "request":
        if args.issue_number is None or args.model is None:
            parser.error("request requires --issue-number and --model")
        return request(
            site_config_path=args.site_config,
            output_dir=args.output_dir,
            issue_number=args.issue_number,
            model=args.model,
        )
    return next_source(
        site_config_path=args.site_config,
        output_dir=args.output_dir,
        issue_number=args.issue_number,
        model=args.model,
    )
