"""Maintainer-only R7 voice-profile and calibration ledger commands."""

from __future__ import annotations

import argparse
import base64
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from citypods.speakers import (
    body_key,
    calibration_cell,
    load_registry,
    load_turn_evidence,
    refresh_membership_status,
    save_evaluation,
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
    _record_calibration(
        state,
        city=args.city,
        body=args.body,
        engine_recipe=args.engine_recipe,
        candidate_id=args.candidate_id,
        correct=args.correct == "yes",
        reviewer=args.reviewer,
        review_id=args.review_id,
    )
    save_evaluation(args.state, state)
    cell = calibration_cell(args.city, args.body, args.engine_recipe)
    print(json.dumps({"stored": True, "cell": cell}))
    return 0


def _record_calibration(
    state: dict,
    *,
    city: str,
    body: str,
    engine_recipe: str,
    candidate_id: str,
    correct: bool,
    reviewer: str,
    review_id: str,
) -> None:
    rows = state.setdefault("reviews", [])
    row = {
        "review_id": review_id,
        "cell": calibration_cell(city, body, engine_recipe),
        "correct": correct,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "reviewer": reviewer,
        "candidate_id": candidate_id,
    }
    prior = next((item for item in rows if item.get("review_id") == review_id), None)
    if prior:
        comparable = {key: value for key, value in row.items() if key != "reviewed_at"}
        if any(prior.get(key) != value for key, value in comparable.items()):
            raise ValueError(f"conflicting replay for review id {review_id!r}")
    if prior is None:
        rows.append(row)
    return None


def _review_body(candidate: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(candidate, sort_keys=True).encode()).decode()
    if candidate.get("kind") == "chair-reference":
        return (
            f"<!-- r7-reference-candidate-b64: {encoded} -->\n"
            f"# R7 chair-introduction reference review: `{candidate['candidate_id']}`\n\n"
            f"Meeting: **{candidate.get('episode_title') or candidate.get('episode_uid')}** "
            f"({candidate.get('city_slug')}, {candidate.get('cue_start')}–"
            f"{candidate.get('cue_end')} seconds)\n\n"
            f"Cue type: **{candidate.get('cue_kind')}**\n\n"
            f"Transcript cue: “{candidate.get('cue_text')}”\n\n"
            f"Proposed official: **{candidate.get('display_name')}**\n\n"
            f"Target speaker turn: {candidate.get('start')}–{candidate.get('end')} seconds\n\n"
            "- [ ] Approve as a golden voice reference\n- [ ] Reject\n\n"
            "Approve only when the cue clearly introduces the person who speaks in the target "
            "turn. "
            "Then comment `/speaker-ingest`. The issue omits voice embeddings and match scores.\n"
        )
    return (
        f"<!-- r7-shadow-candidate-b64: {encoded} -->\n"
        f"# R7 speaker shadow-match review: `{candidate['candidate_id']}`\n\n"
        f"Meeting: **{candidate.get('episode_title') or candidate.get('episode_uid')}** "
        f"({candidate.get('city_slug')}, {candidate.get('start')}–"
        f"{candidate.get('end')} seconds)\n\n"
        f"Proposed recurring official: **{candidate.get('display_name')}**\n\n"
        "- [ ] Correct\n- [ ] Incorrect\n\n"
        "Check exactly one box, then comment `/speaker-ingest`. This issue intentionally omits "
        "voice embeddings and numerical match scores.\n"
    )


def package(args: argparse.Namespace) -> int:
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state
    from citypods.storage import make_storage

    site = load_site_config(args.site_config)
    state_dir = resolve_state_dir(site, Path(args.output_dir))
    storage = make_storage(site, site.get("base_url", ""), Path(args.output_dir))
    speaker_config = site.get("speakers") or {}
    state_path = state_dir / str(speaker_config.get("evaluation_state_path"))
    registry_path = state_dir / str(speaker_config.get("registry_path"))
    evidence_path = state_dir / str(speaker_config.get("turn_evidence_path"))
    pull_state(storage, state_dir, only_paths=[state_path, registry_path, evidence_path])
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"candidates": {}, "reviews": []}
    )
    reviewed = {
        str(row.get("candidate_id")) for row in state.get("reviews", []) if isinstance(row, dict)
    }
    reviewed.update(
        str(row.get("candidate_id"))
        for row in state.get("reference_reviews", [])
        if isinstance(row, dict)
    )
    candidates = [
        value
        for key, value in (state.get("candidates") or {}).items()
        if key not in reviewed and isinstance(value, dict)
    ]
    candidates.extend(
        value
        for key, value in (state.get("reference_candidates") or {}).items()
        if key not in reviewed and isinstance(value, dict)
    )
    candidates.sort(key=lambda row: str(row.get("candidate_id")))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    children = []
    limit = args.limit
    if limit is None:
        limit = int((site.get("speakers") or {}).get("weekly_review_limit", 8))
    selected = candidates[: max(0, limit)]
    for candidate in selected:
        candidate_id = str(candidate["candidate_id"])
        body_file = f"{candidate_id}.md"
        (out_dir / body_file).write_text(_review_body(candidate), encoding="utf-8")
        kind = str(candidate.get("kind") or "shadow-match")
        title_prefix = (
            "R7 chair reference" if kind == "chair-reference" else "R7 speaker shadow sample"
        )
        children.append(
            {
                "candidate_id": candidate_id,
                "body_file": body_file,
                "kind": kind,
                "title": f"{title_prefix} {candidate_id}",
            }
        )
    counts = {
        "shadow_matches": sum(row.get("kind") != "chair-reference" for row in selected),
        "chair_references": sum(row.get("kind") == "chair-reference" for row in selected),
    }
    (out_dir / "parent.md").write_text(
        "# R7 speaker calibration review batch\n\n"
        "Review each child issue and check exactly one outcome. Shadow matches use **Correct** or "
        "**Incorrect**. Chair/title-led introductions use **Approve as a golden voice reference** "
        "or **Reject**. Comment `/speaker-ingest` after checking a box.\n\n"
        f"This batch contains {counts['shadow_matches']} shadow match(es) and "
        f"{counts['chair_references']} chair/title reference candidate(s).\n\n"
        "Reference approval adds only the diarizer's private embedding to the city/body registry; "
        "it does not publish a name or expose voice data.\n",
        encoding="utf-8",
    )
    (out_dir / "review-batch.json").write_text(
        json.dumps({"version": 1, "children": children}, indent=2) + "\n", encoding="utf-8"
    )
    print(f"packaged {len(children)} R7 speaker review issue(s)")
    return 0


def ingest(args: argparse.Namespace) -> int:
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state, push_state
    from citypods.storage import make_storage

    body = Path(args.issue_body_file).read_text(encoding="utf-8")
    match = re.search(r"<!-- r7-(?:shadow|reference)-candidate-b64: ([A-Za-z0-9_=-]+) -->", body)
    if not match:
        raise ValueError("missing trusted R7 speaker review candidate payload")
    candidate = json.loads(base64.urlsafe_b64decode(match.group(1)).decode())
    is_reference = candidate.get("kind") == "chair-reference"
    outcomes = (
        ("Approve as a golden voice reference", "Reject")
        if is_reference
        else (
            "Correct",
            "Incorrect",
        )
    )
    checked = [label for label in outcomes if f"- [x] {label.lower()}" in body.lower()]
    if len(checked) != 1:
        raise ValueError("select exactly one R7 speaker review outcome")
    site = load_site_config(args.site_config)
    state_dir = resolve_state_dir(site, Path(args.output_dir))
    storage = make_storage(site, site.get("base_url", ""), Path(args.output_dir))
    speaker_config = site.get("speakers") or {}
    state_path = state_dir / str(speaker_config.get("evaluation_state_path"))
    registry_path = state_dir / str(speaker_config.get("registry_path"))
    evidence_path = state_dir / str(speaker_config.get("turn_evidence_path"))
    pull_state(storage, state_dir, only_paths=[state_path, registry_path, evidence_path])
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"candidates": {}, "reviews": []}
    )
    ledger = state.get("reference_candidates") if is_reference else state.get("candidates")
    current = (ledger or {}).get(candidate.get("candidate_id"))
    fields = (
        "candidate_id",
        "city_slug",
        "body",
        "engine_recipe",
        "episode_uid",
        "start",
        "end",
    )
    if is_reference:
        fields += ("kind", "display_name", "cue_start", "cue_end", "cue_text", "cue_kind")
    else:
        fields += ("speaker_id",)
    if not isinstance(current, dict) or any(
        current.get(key) != candidate.get(key) for key in fields
    ):
        raise ValueError("R7 review payload differs from the private candidate ledger")
    if is_reference:
        registry = load_registry(registry_path)
        evidence = load_turn_evidence(evidence_path)
        _record_reference_review(
            state,
            registry,
            evidence,
            current,
            approved=checked[0] == outcomes[0],
            reviewer=args.actor,
            review_id=f"github-issue-{args.issue_number}",
        )
        save_registry(registry_path, registry)
        save_evaluation(state_path, state)
        push_state(
            storage,
            state_dir,
            only_paths=[
                str(speaker_config.get("evaluation_state_path")),
                str(speaker_config.get("registry_path")),
            ],
        )
        print(json.dumps({"stored": True, "candidate_id": current["candidate_id"]}))
        return 0
    _record_calibration(
        state,
        city=str(current["city_slug"]),
        body=str(current.get("body") or ""),
        engine_recipe=str(current["engine_recipe"]),
        candidate_id=str(current["candidate_id"]),
        correct=checked[0] == "Correct",
        reviewer=args.actor,
        review_id=f"github-issue-{args.issue_number}",
    )
    save_evaluation(state_path, state)
    push_state(
        storage,
        state_dir,
        only_paths=[str((site.get("speakers") or {}).get("evaluation_state_path"))],
    )
    print(json.dumps({"stored": True, "candidate_id": current["candidate_id"]}))
    return 0


def _record_reference_review(
    state: dict,
    registry: dict,
    evidence: dict,
    candidate: dict,
    *,
    approved: bool,
    reviewer: str,
    review_id: str,
) -> None:
    """Persist a chair/title review and optionally add its private embedding to the registry."""
    rows = state.setdefault("reference_reviews", [])
    row = {
        "review_id": review_id,
        "candidate_id": candidate["candidate_id"],
        "approved": approved,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "reviewer": reviewer,
    }
    prior = next((item for item in rows if item.get("review_id") == review_id), None)
    if prior:
        comparable = {key: value for key, value in row.items() if key != "reviewed_at"}
        if any(prior.get(key) != value for key, value in comparable.items()):
            raise ValueError(f"conflicting replay for reference review id {review_id!r}")
        return
    rows.append(row)
    if not approved:
        return
    episode = (evidence.get("episodes") or {}).get(candidate.get("episode_uid"), {})
    turns = episode.get("turns") if isinstance(episode, dict) else None
    match = next(
        (
            turn
            for turn in turns or []
            if isinstance(turn, dict)
            and abs(float(turn.get("start", -1)) - float(candidate["start"])) < 0.01
            and abs(float(turn.get("end", -1)) - float(candidate["end"])) < 0.01
            and str(turn.get("cluster")) == str(candidate.get("cluster"))
        ),
        None,
    )
    if not isinstance(match, dict) or not isinstance(match.get("embedding"), list):
        raise ValueError("approved reference has no matching private turn embedding")
    ident = speaker_id(
        str(candidate["city_slug"]),
        str(candidate.get("body") or ""),
        str(candidate["display_name"]),
    )
    person = registry.setdefault("people", {}).setdefault(
        ident,
        {
            "speaker_id": ident,
            "display_name": candidate["display_name"],
            "aliases": [],
            "body_key": body_key(str(candidate["city_slug"]), str(candidate.get("body") or "")),
            "membership": {},
            "references": [],
            "status": "probable",
        },
    )
    references = person.setdefault("references", [])
    if not any(
        ref.get("episode_uid") == candidate.get("episode_uid")
        and abs(float(ref.get("start", -1)) - float(candidate["start"])) < 0.01
        for ref in references
        if isinstance(ref, dict)
    ):
        reference = {
            "episode_uid": candidate["episode_uid"],
            "start": candidate["start"],
            "end": candidate["end"],
            "text_hash": candidate.get("transcript_text_hash"),
            "embedding": match["embedding"],
            "embedding_recipe": candidate["engine_recipe"],
            "approved_by": reviewer,
            "approved_at": datetime.now(UTC).isoformat(),
        }
        references.append(reference)
        registry.setdefault("history", []).append(
            {"kind": "golden-reference", "speaker_id": ident, **reference}
        )
    refresh_membership_status(registry)


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
    package_parser = sub.add_parser("package", help="package bounded weekly speaker review issues")
    package_parser.add_argument("--site-config", default="config/site_config.yml")
    package_parser.add_argument("--output-dir", default="docs")
    package_parser.add_argument("--out-dir", required=True)
    package_parser.add_argument("--limit", type=int, default=None)
    ingest_parser = sub.add_parser("ingest", help="ingest a trusted GitHub speaker review issue")
    ingest_parser.add_argument("--site-config", default="config/site_config.yml")
    ingest_parser.add_argument("--output-dir", default="docs")
    ingest_parser.add_argument("--issue-number", required=True, type=int)
    ingest_parser.add_argument("--issue-body-file", required=True)
    ingest_parser.add_argument("--actor", required=True)
    args = parser.parse_args(argv)
    if args.command == "approve-reference":
        return approve_reference(args)
    if args.command == "reject-reference":
        return reject_reference(args)
    if args.command == "resolve-alias":
        return resolve_alias(args)
    if args.command == "package":
        return package(args)
    if args.command == "ingest":
        return ingest(args)
    return record_calibration(args)


__all__ = ["main"]
