"""Maintainer-only R7 voice-profile and calibration ledger commands."""

from __future__ import annotations

import argparse
import base64
import collections
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from citypods.review_issues import render_decision_block, require_one_decision
from citypods.speakers import (
    body_key,
    calibration_cell,
    load_registry,
    load_turn_evidence,
    pilot_selected,
    refresh_membership_status,
    save_evaluation,
    save_registry,
    speaker_id,
    valid_speaker_id,
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
    if args.speaker_id and not valid_speaker_id(args.speaker_id):
        raise ValueError("--speaker-id must be an opaque spk-<16 lowercase hex> identifier")
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
        capture_context=args.capture_context,
    )
    save_evaluation(args.state, state)
    cell = calibration_cell(
        args.city, args.body, args.engine_recipe, capture_context=args.capture_context
    )
    print(json.dumps({"stored": True, "cell": cell}))
    return 0


def record_benchmark(args: argparse.Namespace) -> int:
    """Record a private gold-set engine comparison for this calibration cell.

    No longer a publish precondition: review/31 §C.4.11 retired the per-cell benchmark gate when
    §A.1a made engine selection a single global decision, and `citypods.naming` now owns
    admission. Kept as the durable record of a comparison actually run — nothing reads these rows
    to decide whether a name may be published.
    """
    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        state = {"version": 1, "reviews": []}
    if not isinstance(state, dict):
        raise ValueError("speaker evaluation state must be a JSON object")
    cell = calibration_cell(
        args.city, args.body, args.engine_recipe, capture_context=args.capture_context
    )
    row = {
        "cell": cell,
        "selected_engine": args.selected_engine,
        "report_hash": args.report_hash,
        "reviewer": args.reviewer,
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    benchmarks = state.setdefault("benchmarks", [])
    prior = next(
        (
            item
            for item in benchmarks
            if isinstance(item, dict)
            and item.get("cell") == cell
            and item.get("report_hash") == args.report_hash
        ),
        None,
    )
    if prior is None:
        benchmarks.append(row)
    elif any(prior.get(key) != value for key, value in row.items() if key != "recorded_at"):
        raise ValueError("conflicting benchmark record for this calibration cell/report")
    save_evaluation(args.state, state)
    print(json.dumps({"stored": True, "cell": cell, "selected_engine": args.selected_engine}))
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
    capture_context: str,
    combination_key: str | None = None,
    tier: str | None = None,
) -> None:
    rows = state.setdefault("reviews", [])
    row = {
        "review_id": review_id,
        "cell": calibration_cell(city, body, engine_recipe, capture_context=capture_context),
        "correct": correct,
        "reviewed_at": datetime.now(UTC).isoformat(),
        "reviewer": reviewer,
        "candidate_id": candidate_id,
    }
    # Snapshot what the reviewer actually judged. `naming_candidate_id` deliberately excludes the
    # signal set, so a later projection rewrites that row's `combination_key` and `tier` under the
    # same id -- and the precision table, which joins verdicts to the *current* row, would then
    # credit this ruling to a combination nobody ever reviewed. Gaining a voice reference or
    # receiving minutes would silently move evidence between buckets.
    if combination_key:
        row["combination_key"] = combination_key
    if tier:
        row["tier"] = tier
    prior = next((item for item in rows if item.get("review_id") == review_id), None)
    if prior:
        comparable = {key: value for key, value in row.items() if key != "reviewed_at"}
        if any(prior.get(key) != value for key, value in comparable.items()):
            raise ValueError(f"conflicting replay for review id {review_id!r}")
    if prior is None:
        rows.append(row)
    return None


# Which private ledger holds each reviewable candidate class, which fields its issue payload
# carries (and must still match byte-for-byte at ingest), and the decision labels a reviewer sees.
#
# A table rather than three parallel `if kind == ...` chains, because that shape had already
# drifted: `self-introduction` candidates are written to `reference_candidates` alongside
# `chair-reference` ones, but only the latter string was recognised -- so a self-introduction
# rendered as a shadow-match issue and then failed ingest against a ledger it was never in.
# Classes are keyed by ledger membership; adding a cue kind cannot silently reopen that hole.
_REFERENCE_KINDS = frozenset({"chair-reference", "self-introduction"})
_COMMON_PAYLOAD_FIELDS = (
    "candidate_id",
    "city_slug",
    "body",
    "engine_recipe",
    "capture_context",
    "episode_uid",
)
_REVIEW_CLASSES: dict[str, dict[str, Any]] = {
    "reference": {
        "ledger": "reference_candidates",
        "marker": "r7-reference-candidate-b64",
        "outcomes": ("Approve as a golden voice reference", "Reject"),
        "title_prefix": "R7 chair reference",
        "fields": _COMMON_PAYLOAD_FIELDS
        + (
            "kind",
            "display_name",
            "start",
            "end",
            "cue_start",
            "cue_end",
            "cue_text",
            "cue_kind",
            "embedding_recipe",
            "cluster",
            "transcript_text_hash",
        ),
    },
    "naming": {
        "ledger": "naming_candidates",
        "marker": "r7-naming-candidate-b64",
        "outcomes": ("Correct", "Incorrect"),
        "title_prefix": "R7 speaker name",
        # No start/end: a fused candidate is a claim about a whole cluster, not one turn.
        "fields": _COMMON_PAYLOAD_FIELDS + ("kind", "display_name", "cluster"),
        # Carried in the issue payload but NOT verified against the ledger, and that distinction
        # is the whole point: `naming_candidate_id` excludes the signal set on purpose, so a
        # re-projection between packaging and ingest can legitimately change these under the same
        # id. Verifying them would reject a reviewer's ruling as tampering for the system working
        # as designed; omitting them entirely would leave the verdict with no record of what was
        # judged. Carried, they answer "what did the human actually rule on".
        "carry": ("tier", "combination_key"),
    },
    "shadow": {
        "ledger": "candidates",
        "marker": "r7-shadow-candidate-b64",
        "outcomes": ("Correct", "Incorrect"),
        "title_prefix": "R7 speaker shadow sample",
        "fields": _COMMON_PAYLOAD_FIELDS
        + ("start", "end", "speaker_id", "display_name", "transcript_text_hash"),
    },
}


def _review_class(candidate: Mapping[str, object]) -> str:
    """Which review class a candidate belongs to, by the ledger that holds it."""
    kind = str(candidate.get("kind") or "")
    if kind in _REFERENCE_KINDS:
        return "reference"
    if kind == "naming":
        return "naming"
    return "shadow"


def _review_payload(candidate: Mapping[str, object]) -> dict[str, object]:
    """Return the payload embedded in a review issue.

    `fields` are rechecked against the private ledger at ingest; `carry` fields ride along
    unverified because they may legitimately change between packaging and ingest.
    """
    spec = _REVIEW_CLASSES[_review_class(candidate)]
    keys = (*spec["fields"], *spec.get("carry", ()))
    return {key: candidate[key] for key in keys if key in candidate}


def _review_body(candidate: dict) -> str:
    review_class = _review_class(candidate)
    spec = _REVIEW_CLASSES[review_class]
    payload = _review_payload(candidate)
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    header = f"<!-- {spec['marker']}: {encoded} -->\n"
    meeting = (
        f"Meeting: **{candidate.get('episode_title') or candidate.get('episode_uid')}** "
        f"({candidate.get('city_slug')})\n\n"
    )
    if review_class == "reference":
        cue_lead = (
            "The chair introduces"
            if candidate.get("kind") == "chair-reference"
            else "The speaker introduces themselves as"
        )
        return (
            f"{header}"
            f"# R7 voice-reference review: `{candidate['candidate_id']}`\n\n"
            f"{meeting}"
            f"Cue type: **{candidate.get('cue_kind')}** "
            f"({candidate.get('cue_start')}–{candidate.get('cue_end')} seconds)\n\n"
            f"Transcript cue: “{candidate.get('cue_text')}”\n\n"
            f"{cue_lead}: **{candidate.get('display_name')}**\n\n"
            f"Target speaker turn: {candidate.get('start')}–{candidate.get('end')} seconds\n\n"
            f"{render_decision_block(spec['outcomes'])}\n\n"
            "Approve only when the cue clearly identifies the person who speaks in the target "
            "turn. The issue omits voice embeddings and match scores.\n"
        )
    if review_class == "naming":
        signals = ", ".join(str(item) for item in candidate.get("signals") or ()) or "none"
        spans = (
            ", ".join(
                f"{row.get('start')}-{row.get('end')}s"
                for row in candidate.get("turns") or ()
                if isinstance(row, Mapping)
            )
            or "not recorded"
        )
        cues = "\n".join(
            f"- **{row.get('kind')}** at {row.get('start')}s: “{row.get('text')}”"
            for row in candidate.get("cues") or ()
            if isinstance(row, Mapping)
        )
        return (
            f"{header}"
            f"# R7 speaker name review: `{candidate['candidate_id']}`\n\n"
            f"{meeting}"
            f"Proposed name for speaker cluster `{candidate.get('cluster')}`: "
            f"**{candidate.get('display_name')}**\n\n"
            f"Tier: **{candidate.get('tier')}** · agreeing signals: **{signals}**\n\n"
            f"This cluster speaks at: {spans}\n\n"
            + (f"Supporting transcript cue(s):\n{cues}\n\n" if cues else "")
            + f"{render_decision_block(spec['outcomes'])}\n\n"
            "Your ruling is what teaches the gate which signal combinations can be trusted "
            "unattended, so judge the name itself, not how plausible the combination looks. "
            "The issue omits voice embeddings and numerical match scores.\n"
        )
    return (
        f"{header}"
        f"# R7 speaker shadow-match review: `{candidate['candidate_id']}`\n\n"
        f"{meeting}"
        f"Speaker turn: {candidate.get('start')}–{candidate.get('end')} seconds\n\n"
        f"Proposed recurring official: **{candidate.get('display_name')}**\n\n"
        f"{render_decision_block(spec['outcomes'])}\n\n"
        "Check exactly one box. This issue intentionally omits "
        "voice embeddings and numerical match scores.\n"
    )


def _speaker_state_paths(
    site: Mapping[str, object], state_dir: Path
) -> tuple[Path, Path, Path, list[str]]:
    speaker_config = site.get("speakers")
    if not isinstance(speaker_config, Mapping):
        raise ValueError("site speakers configuration is required")
    keys = ("evaluation_state_path", "registry_path", "turn_evidence_path")
    values: dict[str, str] = {}
    for key in keys:
        value = speaker_config.get(key)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"site speakers.{key} is required")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"site speakers.{key} must be a relative state path")
        values[key] = value
    relative_paths = [values[key] for key in keys]
    return (
        state_dir / values["evaluation_state_path"],
        state_dir / values["registry_path"],
        state_dir / values["turn_evidence_path"],
        relative_paths,
    )


# How many verdicts a class is worth per unit of reviewer attention, most valuable first. The
# weekly limit is small (8 by default), so this ordering -- not the size of the backlog -- is what
# actually determines how fast the gate learns.
_REVIEW_CLASS_RANK = {
    # A reference approval mints a voice profile, which then names its subject in *every* past
    # and future meeting automatically. Highest leverage per review by a wide margin.
    "reference": 0,
    # A naming verdict is the only input to the precision table (review/31 §C.4.4): without these
    # no combination ever becomes trusted and staff auto-naming never opens.
    "naming": 1,
    # A shadow match confirms one already-matched turn. Useful, but it teaches the least.
    "shadow": 2,
}


def _review_rank(candidate: Mapping[str, object]) -> tuple:
    """Order the review queue by expected value, not by candidate-id hash.

    Within a class, prefer the most-corroborated candidate: more agreeing signals means the
    reviewer is more likely to be confirming than correcting, which is both faster for them and
    what §C.4.5's "highest-confidence matches publish first" requires. `candidate_id` only breaks
    ties, so the ordering stays deterministic across runs.
    """
    review_class = _review_class(candidate)
    signals = candidate.get("signals")
    signal_count = len(signals) if isinstance(signals, list) else 0
    return (
        _REVIEW_CLASS_RANK.get(review_class, len(_REVIEW_CLASS_RANK)),
        -signal_count,
        str(candidate.get("candidate_id") or ""),
    )


# Share of each batch held for naming verdicts while any are waiting. References rank first
# because one approval mints a reusable voice profile -- but *only* naming verdicts populate the
# precision table, so a steady arrival of eight or more references per week would fill every batch
# and no signal combination could ever become trusted. Priority still decides the ordering; this
# only stops the top class from consuming the whole batch.
_NAMING_BATCH_RESERVE = 0.5


def _select_batch(candidates: list[dict], limit: int) -> list[dict]:
    """Take `limit` candidates in rank order, reserving capacity for naming verdicts.

    Without the reserve the ranking is self-defeating: approved voice profiles cannot publish
    anything while the combination they would be used under remains untrusted, and only the
    reviews being starved can make it trusted.
    """
    if limit <= 0:
        return []
    naming = [row for row in candidates if _review_class(row) == "naming"]
    if not naming or len(candidates) <= limit:
        return candidates[:limit]
    reserved = min(len(naming), max(1, int(limit * _NAMING_BATCH_RESERVE)))
    chosen = naming[:reserved]
    chosen_ids = {id(row) for row in chosen}
    for row in candidates:
        if len(chosen) >= limit:
            break
        if id(row) not in chosen_ids:
            chosen.append(row)
    return sorted(chosen, key=_review_rank)


def package(args: argparse.Namespace) -> int:
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state
    from citypods.storage import make_storage

    site = load_site_config(args.site_config)
    state_dir = resolve_state_dir(site, Path(args.output_dir))
    storage = make_storage(site, site.get("base_url", ""), Path(args.output_dir))
    state_path, registry_path, evidence_path, relative_paths = _speaker_state_paths(site, state_dir)
    pull_state(storage, state_dir, only_paths=relative_paths)
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
    candidate_pool = [
        value
        for spec in _REVIEW_CLASSES.values()
        for value in (state.get(spec["ledger"]) or {}).values()
        if isinstance(value, dict)
    ]
    candidates = [
        row for row in candidate_pool if str(row.get("candidate_id") or "") not in reviewed
    ]
    candidates.sort(key=_review_rank)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    children = []
    limit = args.limit
    if limit is None:
        limit = int((site.get("speakers") or {}).get("weekly_review_limit", 8))
    selected = _select_batch(candidates, max(0, limit))
    for candidate in selected:
        candidate_id = str(candidate["candidate_id"])
        body_file = f"{candidate_id}.md"
        (out_dir / body_file).write_text(_review_body(candidate), encoding="utf-8")
        kind = str(candidate.get("kind") or "shadow-match")
        review_class = _review_class(candidate)
        children.append(
            {
                "candidate_id": candidate_id,
                "body_file": body_file,
                "kind": kind,
                "title": f"{_REVIEW_CLASSES[review_class]['title_prefix']} {candidate_id}",
            }
        )
    counts = collections.Counter(_review_class(row) for row in selected)
    (out_dir / "parent.md").write_text(
        "# R7 speaker calibration review batch\n\n"
        "Review each child issue and check exactly one outcome. Name and shadow-match reviews use "
        "**Correct** or **Incorrect**. Introduction cues use **Approve as a golden voice "
        "reference** or **Reject**.\n\n"
        f"This batch contains {counts['reference']} introduction reference candidate(s), "
        f"{counts['naming']} proposed name(s), and {counts['shadow']} shadow match(es).\n\n"
        "Reference approval adds only the diarizer's private embedding to the city/body registry; "
        "it does not publish a name or expose voice data. Name rulings are what teach the gate "
        "which signal combinations may later be trusted without a human.\n",
        encoding="utf-8",
    )
    speakers = site.get("speakers") or {}
    pilot_configured = bool(speakers.get("pilot_bodies"))
    saw_pilot = any(
        pilot_selected(site.get("speakers") or {}, str(row.get("city_slug") or ""), row.get("body"))
        for row in candidate_pool
    )
    manifest: dict[str, object] = {"version": 1, "children": children}
    if pilot_configured and not saw_pilot:
        manifest["reasons"] = ["configured pilot body not processed"]
    (out_dir / "review-batch.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(f"packaged {len(children)} R7 speaker review issue(s)")
    return 0


def ingest(args: argparse.Namespace) -> int:
    from citypods.config import load_site_config
    from citypods.state import resolve_state_dir
    from citypods.statesync import pull_state, push_state
    from citypods.storage import make_storage

    body = Path(args.issue_body_file).read_text(encoding="utf-8")
    match = re.search(
        r"<!-- r7-(?:shadow|reference|naming)-candidate-b64: ([A-Za-z0-9_=-]+) -->", body
    )
    if not match:
        raise ValueError("missing trusted R7 speaker review candidate payload")
    candidate = json.loads(base64.urlsafe_b64decode(match.group(1)).decode())
    review_class = _review_class(candidate)
    spec = _REVIEW_CLASSES[review_class]
    is_reference = review_class == "reference"
    outcomes = spec["outcomes"]
    label = require_one_decision(body, outcomes)
    site = load_site_config(args.site_config)
    state_dir = resolve_state_dir(site, Path(args.output_dir))
    storage = make_storage(site, site.get("base_url", ""), Path(args.output_dir))
    state_path, registry_path, evidence_path, relative_paths = _speaker_state_paths(site, state_dir)
    pull_state(storage, state_dir, only_paths=relative_paths)
    state = (
        json.loads(state_path.read_text())
        if state_path.exists()
        else {"candidates": {}, "reviews": []}
    )
    ledger = state.get(spec["ledger"])
    current = (ledger or {}).get(candidate.get("candidate_id"))
    if not isinstance(current, dict) or any(
        current.get(key) != candidate.get(key) for key in spec["fields"]
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
            approved=label == outcomes[0],
            reviewer=args.actor,
            review_id=f"github-issue-{args.issue_number}",
        )
        save_registry(registry_path, registry)
        save_evaluation(state_path, state)
        push_state(
            storage,
            state_dir,
            only_paths=relative_paths[:2],
        )
        print(json.dumps({"stored": True, "candidate_id": current["candidate_id"]}))
        return 0
    _record_calibration(
        state,
        city=str(current["city_slug"]),
        body=str(current.get("body") or ""),
        engine_recipe=str(current["engine_recipe"]),
        candidate_id=str(current["candidate_id"]),
        correct=label == "Correct",
        reviewer=args.actor,
        review_id=f"github-issue-{args.issue_number}",
        capture_context=str(current["capture_context"]),
        # From `candidate` -- the verified issue payload the reviewer judged -- not from
        # `current`, the mutable ledger row, which a later projection may already have rewritten.
        combination_key=str(candidate.get("combination_key") or "") or None,
        tier=str(candidate.get("tier") or "") or None,
    )
    save_evaluation(state_path, state)
    push_state(
        storage,
        state_dir,
        only_paths=relative_paths[:1],
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
            "embedding_recipe": candidate.get("embedding_recipe") or candidate["engine_recipe"],
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
    calibration.add_argument("--capture-context", required=True)
    shadow = sub.add_parser("review-shadow", help="record one reviewed shadow identity match")
    shadow.add_argument("--state", required=True, type=Path)
    shadow.add_argument("--city", required=True)
    shadow.add_argument("--body", required=True)
    shadow.add_argument("--engine-recipe", required=True)
    shadow.add_argument("--candidate-id", required=True)
    shadow.add_argument("--correct", choices=("yes", "no"), required=True)
    shadow.add_argument("--reviewer", required=True)
    shadow.add_argument("--review-id", required=True)
    shadow.add_argument("--capture-context", required=True)
    benchmark = sub.add_parser("record-benchmark", help="record a private gold-benchmark decision")
    benchmark.add_argument("--state", required=True, type=Path)
    benchmark.add_argument("--city", required=True)
    benchmark.add_argument("--body", required=True)
    benchmark.add_argument("--engine-recipe", required=True)
    benchmark.add_argument("--capture-context", required=True)
    # Free-form, not a fixed choice list: it was pinned to ("pyannote", "wespeaker") and so could
    # not name the engine actually in use (review/31 §A.1a selected sherpa-onnx + NeMo
    # TitaNet-Small), which made the durable record this command exists to keep unwritable.
    benchmark.add_argument("--selected-engine", required=True)
    benchmark.add_argument("--report-hash", required=True)
    benchmark.add_argument("--reviewer", required=True)
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
    if args.command == "record-benchmark":
        return record_benchmark(args)
    if args.command == "package":
        return package(args)
    if args.command == "ingest":
        return ingest(args)
    return record_calibration(args)


__all__ = ["main"]
