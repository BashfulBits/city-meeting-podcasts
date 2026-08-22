"""Speaker identity, profile, and public-attribution helpers for R7.

The diarizer answers only "which voice cluster spoke when".  This module keeps the
separate, auditable answer to "may this cluster be shown as a named official".  Raw
reference audio is never persisted here: a profile contains only recipe-versioned
vectors and links back to the already-retained public meeting/time evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

IDENTITY_PIPELINE_VERSION = "1"
MIN_REFERENCE_MEETINGS = 2
MIN_CALIBRATION_DAYS = 30
MIN_CALIBRATION_REVIEWS = 30
REQUIRED_PRECISION = 0.95
PROFILE_REVIEW_ONLY_AFTER_DAYS = 180


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat()


def _norm(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def body_key(city_slug: str, body: str | None) -> str:
    """Stable scope for a body-local identity registry."""
    digest = hashlib.sha1(_norm(body).encode()).hexdigest()[:10]
    return f"{city_slug}:{digest}"


def speaker_id(city_slug: str, body: str | None, display_name: str) -> str:
    """Mint an opaque, body-scoped id only when a reviewer approves a person."""
    digest = hashlib.sha1(f"{body_key(city_slug, body)}:{_norm(display_name)}".encode()).hexdigest()
    return f"spk-{digest[:16]}"


def empty_registry() -> dict[str, Any]:
    return {"version": 1, "people": {}, "history": []}


def load_registry(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return empty_registry()
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read speaker registry {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("speaker registry must be a JSON object")
    value.setdefault("version", 1)
    value.setdefault("people", {})
    value.setdefault("history", [])
    return value


def save_registry(path: Path, registry: Mapping[str, Any]) -> None:
    _save_json(path, registry)


def load_turn_evidence(path: Path) -> dict[str, Any]:
    """Read the private per-turn embedding cache used by reviewer/identity workflows."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "episodes": {}}
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read speaker turn evidence {path}") from exc
    if not isinstance(value, dict):
        raise ValueError("speaker turn evidence must be a JSON object")
    value.setdefault("version", 1)
    value.setdefault("episodes", {})
    return value


def save_turn_evidence(path: Path, evidence: Mapping[str, Any]) -> None:
    """Persist raw embedding vectors only in the private state ledger."""
    _save_json(path, evidence)


def save_evaluation(path: Path, evaluation: Mapping[str, Any]) -> None:
    """Persist the private, append-only shadow-match review ledger."""
    _save_json(path, evaluation)


def shadow_candidate_id(
    *, city_slug: str, body: str | None, episode_uid: str, recipe: str, turn: Mapping[str, Any]
) -> str:
    """Stable, privacy-safe identity for one reviewable shadow voice assignment."""
    payload = {
        "city": city_slug,
        "body": _norm(body),
        "episode_uid": episode_uid,
        "recipe": recipe,
        "start": turn.get("start"),
        "end": turn.get("end"),
        "cluster": turn.get("cluster"),
        "speaker_id": (turn.get("identity") or {}).get("speaker_id"),
    }
    return "r7-" + hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def public_turn(turn: Mapping[str, Any]) -> dict[str, Any]:
    """Remove identity vectors before a diarization artifact can receive a public URL."""
    return {key: value for key, value in turn.items() if key not in {"embedding", "identity"}}


def _save_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(value), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _person_by_name(registry: dict[str, Any], name: str) -> tuple[str, dict[str, Any]] | None:
    wanted = _norm(name)
    for ident, person in (registry.get("people") or {}).items():
        if not isinstance(person, dict):
            continue
        aliases = [person.get("display_name"), *(person.get("aliases") or [])]
        if any(_norm(alias) == wanted for alias in aliases):
            return str(ident), person
    return None


def observe_attendance(
    registry: dict[str, Any],
    *,
    city_slug: str,
    body: str | None,
    episode_uid: str,
    published: datetime,
    roster: Iterable[Mapping[str, Any]],
    votes: Iterable[Mapping[str, Any]] = (),
) -> None:
    """Append official minutes evidence without asserting an elected-office term.

    Attendance is a candidate prior for later matching, never a proof that a person spoke.
    New names deliberately remain un-enrolled until a reviewer creates golden voice references.
    """
    seen: dict[str, str] = {}
    for item in roster:
        name = str(item.get("name") or "").strip()
        if name:
            seen[_norm(name)] = name
    for item in votes:
        for vote in item.get("votes", []) if isinstance(item, Mapping) else []:
            if isinstance(vote, Mapping) and vote.get("member"):
                name = str(vote["member"]).strip()
                seen.setdefault(_norm(name), name)
    for name in seen.values():
        found = _person_by_name(registry, name)
        if found is None:
            ident = speaker_id(city_slug, body, name)
            person = {
                "speaker_id": ident,
                "display_name": name,
                "aliases": [],
                "body_key": body_key(city_slug, body),
                "membership": {
                    "first_seen": published.isoformat(),
                    "last_seen": published.isoformat(),
                },
                "references": [],
                "status": "probable",
            }
            registry.setdefault("people", {})[ident] = person
        else:
            ident, person = found
            membership = person.setdefault("membership", {})
            membership.setdefault("first_seen", published.isoformat())
            membership["last_seen"] = published.isoformat()
        registry.setdefault("history", []).append(
            {
                "kind": "attendance",
                "speaker_id": ident,
                "episode_uid": episode_uid,
                "observed_at": _iso(),
                "published": published.isoformat(),
            }
        )


def refresh_membership_status(registry: dict[str, Any], *, now: datetime | None = None) -> None:
    current = now or _now()
    for person in (registry.get("people") or {}).values():
        if not isinstance(person, dict):
            continue
        last_seen = _parse_time((person.get("membership") or {}).get("last_seen"))
        if last_seen is None:
            person["status"] = "review_only"
        elif current - last_seen > timedelta(days=PROFILE_REVIEW_ONLY_AFTER_DAYS):
            person["status"] = "review_only"
        elif person.get("references"):
            person["status"] = "active"
        else:
            person["status"] = "probable"


def _cosine(left: Iterable[float], right: Iterable[float]) -> float:
    a, b = list(left), list(right)
    if not a or len(a) != len(b):
        return -1.0
    denom = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / denom if denom else -1.0


def qualified_profile(person: Mapping[str, Any]) -> bool:
    """A public auto-match needs references from two distinct meetings."""
    references = [row for row in person.get("references", []) if isinstance(row, Mapping)]
    meetings = {str(row.get("episode_uid") or "") for row in references if row.get("embedding")}
    return person.get("status") == "active" and len(meetings - {""}) >= MIN_REFERENCE_MEETINGS


def profile_matches(
    registry: Mapping[str, Any],
    embedding: Iterable[float],
    *,
    allowed_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Rank eligible profiles by the best approved reference similarity."""
    matches: list[dict[str, Any]] = []
    for ident, person in (registry.get("people") or {}).items():
        if not isinstance(person, Mapping) or (
            allowed_ids is not None and ident not in allowed_ids
        ):
            continue
        if not qualified_profile(person):
            continue
        scores = [
            _cosine(embedding, row.get("embedding") or [])
            for row in person.get("references", [])
            if isinstance(row, Mapping)
        ]
        if scores:
            matches.append(
                {
                    "speaker_id": ident,
                    "display_name": person.get("display_name"),
                    "score": max(scores),
                }
            )
    return sorted(matches, key=lambda row: float(row["score"]), reverse=True)


def calibration_cell(city_slug: str, body: str | None, engine_recipe: str) -> str:
    return "|".join((city_slug, _norm(body), engine_recipe))


def auto_publish_allowed(
    state: Mapping[str, Any], *, cell: str, now: datetime | None = None
) -> bool:
    """Require the locked 30-day/30-review/95%-precision calibration policy."""
    rows = [
        row
        for row in state.get("reviews", [])
        if isinstance(row, Mapping) and row.get("cell") == cell
    ]
    if len(rows) < MIN_CALIBRATION_REVIEWS:
        return False
    dates = [_parse_time(row.get("reviewed_at")) for row in rows]
    valid_dates = [value for value in dates if value]
    if not valid_dates or (now or _now()) - min(valid_dates) < timedelta(days=MIN_CALIBRATION_DAYS):
        return False
    correct = sum(bool(row.get("correct")) for row in rows)
    return correct / len(rows) >= REQUIRED_PRECISION


def assign_turn(
    turn: Mapping[str, Any],
    matches: list[Mapping[str, Any]],
    *,
    publish: bool,
    minimum_score: float = 0.75,
) -> dict[str, Any]:
    """Attach the best unambiguous identity without ever exposing a raw embedding."""
    result = dict(turn)
    if not matches:
        return result
    best = matches[0]
    runner_up = float(matches[1].get("score") or -1.0) if len(matches) > 1 else -1.0
    score = float(best.get("score") or -1.0)
    # The calibration policy controls publication; the gap still prevents a numerical tie from
    # becoming an identity candidate in the review queue.
    if score < minimum_score or score <= runner_up + 0.02:
        return result
    result["identity"] = {
        "speaker_id": best.get("speaker_id"),
        "display_name": best.get("display_name"),
        "status": "provisional" if publish else "shadow",
        "method": "voice-profile",
        "match_score": score,
    }
    return result


def quote_attribution(
    candidate: Mapping[str, Any], turns: Iterable[Mapping[str, Any]]
) -> dict[str, Any] | None:
    """Project an identity only when one non-overlapped turn wholly covers a quote."""
    start, end = candidate.get("start"), candidate.get("end")
    if not isinstance(start, int | float) or not isinstance(end, int | float):
        return None
    covering = [
        turn
        for turn in turns
        if isinstance(turn, Mapping)
        and not turn.get("overlap")
        and isinstance(turn.get("start"), int | float)
        and isinstance(turn.get("end"), int | float)
        and float(turn["start"]) <= float(start)
        and float(turn["end"]) >= float(end)
        and isinstance(turn.get("identity"), Mapping)
        and turn["identity"].get("status") in {"provisional", "confirmed"}
    ]
    if len(covering) != 1:
        return None
    identity = covering[0]["identity"]
    return {
        "speaker_id": identity.get("speaker_id"),
        "display_name": identity.get("display_name"),
        "status": identity.get("status"),
        "method": identity.get("method"),
    }


__all__ = [
    "IDENTITY_PIPELINE_VERSION",
    "MIN_REFERENCE_MEETINGS",
    "assign_turn",
    "auto_publish_allowed",
    "body_key",
    "calibration_cell",
    "empty_registry",
    "load_registry",
    "load_turn_evidence",
    "observe_attendance",
    "profile_matches",
    "public_turn",
    "qualified_profile",
    "quote_attribution",
    "refresh_membership_status",
    "save_registry",
    "save_evaluation",
    "save_turn_evidence",
    "shadow_candidate_id",
    "speaker_id",
]
