"""Maintainer-only R7 voice-profile and calibration ledger commands."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from citypods.speakers import (
    body_key,
    calibration_cell,
    load_registry,
    refresh_membership_status,
    save_registry,
    speaker_id,
)


def _load_vector(path: Path) -> list[float]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(value, dict):
        value = value.get("embedding")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, int | float) for item in value)
    ):
        raise ValueError("--embedding must contain a non-empty JSON numeric vector")
    return [float(item) for item in value]


def approve_reference(args: argparse.Namespace) -> int:
    if args.end <= args.start:
        raise ValueError("golden reference end must be after start")
    registry = load_registry(args.registry)
    ident = args.speaker_id or speaker_id(args.city, args.body, args.name)
    people = registry.setdefault("people", {})
    person = people.setdefault(
        ident,
        {
            "speaker_id": ident,
            "display_name": args.name,
            "aliases": [],
            "body_key": body_key(args.city, args.body),
            "membership": {},
            "references": [],
            "status": "probable",
        },
    )
    vector = _load_vector(args.embedding)
    reference = {
        "episode_uid": args.episode_uid,
        "start": args.start,
        "end": args.end,
        "text_hash": args.text_hash,
        "embedding": vector,
        "embedding_recipe": args.embedding_recipe,
        "approved_by": args.reviewer,
        "approved_at": datetime.now(UTC).isoformat(),
    }
    existing = person.setdefault("references", [])
    if not any(
        row.get("episode_uid") == args.episode_uid and row.get("start") == args.start
        for row in existing
        if isinstance(row, dict)
    ):
        existing.append(reference)
        registry.setdefault("history", []).append(
            {"kind": "golden-reference", "speaker_id": ident, **reference}
        )
    refresh_membership_status(registry)
    save_registry(args.registry, registry)
    print(json.dumps({"speaker_id": ident, "reference_count": len(existing)}))
    return 0


def reject_reference(args: argparse.Namespace) -> int:
    """Record a rejected candidate without ever adding it to a usable voice profile."""
    registry = load_registry(args.registry)
    registry.setdefault("history", []).append(
        {
            "kind": "golden-reference-rejected",
            "speaker_id": args.speaker_id,
            "episode_uid": args.episode_uid,
            "start": args.start,
            "end": args.end,
            "text_hash": args.text_hash,
            "reason": args.reason,
            "reviewer": args.reviewer,
            "reviewed_at": datetime.now(UTC).isoformat(),
        }
    )
    save_registry(args.registry, registry)
    print(json.dumps({"stored": True, "status": "rejected"}))
    return 0


def resolve_alias(args: argparse.Namespace) -> int:
    """Add a reviewer-confirmed spelling/usage alias to one opaque speaker id."""
    registry = load_registry(args.registry)
    person = (registry.get("people") or {}).get(args.speaker_id)
    if not isinstance(person, dict):
        raise ValueError(f"unknown speaker id {args.speaker_id!r}")
    aliases = person.setdefault("aliases", [])
    if args.alias not in aliases:
        aliases.append(args.alias)
        registry.setdefault("history", []).append(
            {
                "kind": "alias-resolved",
                "speaker_id": args.speaker_id,
                "alias": args.alias,
                "reviewer": args.reviewer,
                "resolved_at": datetime.now(UTC).isoformat(),
            }
        )
    save_registry(args.registry, registry)
    print(json.dumps({"speaker_id": args.speaker_id, "aliases": aliases}))
    return 0


def record_calibration(args: argparse.Namespace) -> int:
    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {"version": 1, "reviews": []}
    if not isinstance(state, dict):
        raise ValueError("speaker evaluation state must be a JSON object")
    rows = state.setdefault("reviews", [])
    review_id = args.review_id
    row = {
        "review_id": review_id,
        "cell": calibration_cell(args.city, args.body, args.engine_recipe),
        "correct": args.correct == "yes",
        "reviewed_at": datetime.now(UTC).isoformat(),
        "reviewer": args.reviewer,
        "candidate_id": args.candidate_id,
    }
    prior = next((item for item in rows if item.get("review_id") == review_id), None)
    if prior:
        comparable = {key: value for key, value in row.items() if key != "reviewed_at"}
        if any(prior.get(key) != value for key, value in comparable.items()):
            raise ValueError(f"conflicting replay for review id {review_id!r}")
    if prior is None:
        rows.append(row)
    args.state.parent.mkdir(parents=True, exist_ok=True)
    args.state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"stored": True, "cell": row["cell"]}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citypods speaker-review")
    sub = parser.add_subparsers(dest="command", required=True)
    approve = sub.add_parser("approve-reference", help="approve one clean golden voice turn")
    approve.add_argument("--registry", required=True, type=Path)
    approve.add_argument("--city", required=True)
    approve.add_argument("--body", required=True)
    approve.add_argument("--name", required=True)
    approve.add_argument("--speaker-id")
    approve.add_argument("--episode-uid", required=True)
    approve.add_argument("--start", required=True, type=float)
    approve.add_argument("--end", required=True, type=float)
    approve.add_argument("--text-hash", required=True)
    approve.add_argument("--embedding", required=True, type=Path)
    approve.add_argument("--embedding-recipe", required=True)
    approve.add_argument("--reviewer", required=True)
    reject = sub.add_parser("reject-reference", help="record a rejected golden-turn candidate")
    reject.add_argument("--registry", required=True, type=Path)
    reject.add_argument("--speaker-id", required=True)
    reject.add_argument("--episode-uid", required=True)
    reject.add_argument("--start", required=True, type=float)
    reject.add_argument("--end", required=True, type=float)
    reject.add_argument("--text-hash", required=True)
    reject.add_argument("--reason", required=True)
    reject.add_argument("--reviewer", required=True)
    alias = sub.add_parser("resolve-alias", help="record a reviewer-confirmed speaker alias")
    alias.add_argument("--registry", required=True, type=Path)
    alias.add_argument("--speaker-id", required=True)
    alias.add_argument("--alias", required=True)
    alias.add_argument("--reviewer", required=True)
    calibration = sub.add_parser("record-calibration", help="record one checked identity match")
    calibration.add_argument("--state", required=True, type=Path)
    calibration.add_argument("--city", required=True)
    calibration.add_argument("--body", required=True)
    calibration.add_argument("--engine-recipe", required=True)
    calibration.add_argument("--candidate-id", required=True)
    calibration.add_argument("--correct", choices=("yes", "no"), required=True)
    calibration.add_argument("--reviewer", required=True)
    calibration.add_argument("--review-id", required=True)
    shadow = sub.add_parser("review-shadow", help="record one reviewed shadow identity match")
    shadow.add_argument("--state", required=True, type=Path)
    shadow.add_argument("--city", required=True)
    shadow.add_argument("--body", required=True)
    shadow.add_argument("--engine-recipe", required=True)
    shadow.add_argument("--candidate-id", required=True)
    shadow.add_argument("--correct", choices=("yes", "no"), required=True)
    shadow.add_argument("--reviewer", required=True)
    shadow.add_argument("--review-id", required=True)
    args = parser.parse_args(argv)
    if args.command == "approve-reference":
        return approve_reference(args)
    if args.command == "reject-reference":
        return reject_reference(args)
    if args.command == "resolve-alias":
        return resolve_alias(args)
    return record_calibration(args)


__all__ = ["main"]
