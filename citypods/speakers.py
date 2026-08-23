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
import re
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

_ANNOUNCEMENT_TITLES: tuple[tuple[str, ...], ...] = (
    ("council", "member"),
    ("councilmember",),
    ("councilman",),
    ("councilwoman",),
    ("commissioner",),
    ("mayor",),
    ("vice", "mayor"),
    ("alderman",),
    ("alderwoman",),
    ("trustee",),
    ("supervisor",),
    ("representative",),
    ("senator",),
)
_RECOGNITION_CUES: tuple[tuple[str, ...], ...] = (
    ("the", "chair", "recognizes"),
    ("chair", "recognizes"),
    ("the", "chair", "recognised"),
    ("chair", "recognised"),
    ("i", "recognize"),
    ("i", "recognise"),
    ("chair", "calls", "on"),
    ("the", "chair", "calls", "on"),
)
_NAME_STOP_WORDS = frozenset(
    {
        "about",
        "and",
        "by",
        "for",
        "from",
        "here",
        "is",
        "next",
        "now",
        "of",
        "on",
        "please",
        "speaking",
        "the",
        "to",
        "will",
        "with",
    }
)


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


def pilot_selected(config: Mapping[str, Any], city_slug: str, body: str | None) -> bool:
    """Return whether an opt-in R7 pilot explicitly includes this city/body pair.

    An empty allowlist is deliberately not interpreted as "all".  Public-name calibration is a
    sensitive rollout, so enabling the subsystem without selecting a pilot stays fail-closed.
    """
    for row in config.get("pilot_bodies") or []:
        if not isinstance(row, Mapping):
            continue
        if str(row.get("city") or "") != city_slug:
            continue
        if _norm(row.get("body")) == _norm(body):
            return True
    return False


def speaker_id(city_slug: str, body: str | None, display_name: str) -> str:
    """Mint an opaque, body-scoped id only when a reviewer approves a person."""
    digest = hashlib.sha1(f"{body_key(city_slug, body)}:{_norm(display_name)}".encode()).hexdigest()
    return f"spk-{digest[:16]}"


def valid_speaker_id(value: object) -> bool:
    """Return whether ``value`` is safe for the public speaker-page path segment."""
    return isinstance(value, str) and bool(re.fullmatch(r"spk-[0-9a-f]{16}", value))


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


def reference_candidate_id(
    *,
    city_slug: str,
    body: str | None,
    episode_uid: str,
    recipe: str,
    proposed_name: str,
    cue_start: float,
    turn: Mapping[str, Any],
) -> str:
    """Return a stable id for a chair/title-led golden-reference candidate."""
    payload = {
        "city": city_slug,
        "body": _norm(body),
        "episode_uid": episode_uid,
        "recipe": recipe,
        "proposed_name": _norm(proposed_name),
        "cue_start": cue_start,
        "turn_start": turn.get("start"),
        "turn_end": turn.get("end"),
        "cluster": turn.get("cluster"),
    }
    return "r7-ref-" + hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:20]


def chair_reference_candidates(
    words: Mapping[str, Any], turns: Iterable[Mapping[str, Any]], *, known_names: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Find conservative title/recognition cues followed by a clean diarized turn.

    This is review evidence only.  It never assigns the proposed name to a turn.  The cue can be a
    formal recognition (``the chair recognizes...``) or the shorter title-led announcement common
    in civic recordings (``Commissioner Jane Doe`` / ``Council Member Jane Doe``).  A candidate is
    emitted only when the next non-overlapped turn has a private embedding, so approval can create
    a real golden reference without copying audio into the review issue.
    """
    rows = list(_timed_word_rows(words))
    turn_rows = [row for row in turns if isinstance(row, Mapping)]
    known = {_norm(name): str(name).strip() for name in known_names if str(name).strip()}
    matches: list[dict[str, Any]] = []
    for index in range(len(rows)):
        for cue in _RECOGNITION_CUES:
            if not _sequence_at(rows, index, cue):
                continue
            name, end_index = _name_after(rows, index + len(cue), known)
            if name:
                matches.append(
                    _reference_candidate(
                        rows,
                        turn_rows,
                        index,
                        end_index,
                        name,
                        "chair-recognition",
                    )
                )
        for title in _ANNOUNCEMENT_TITLES:
            if not _sequence_at(rows, index, title):
                continue
            name, end_index = _name_after(rows, index + len(title), known)
            if name:
                matches.append(
                    _reference_candidate(
                        rows,
                        turn_rows,
                        index,
                        end_index,
                        name,
                        "title-announcement",
                    )
                )
    unique: dict[tuple[float, float, str], dict[str, Any]] = {}
    for candidate in matches:
        if not candidate:
            continue
        key = (
            float(candidate["start"]),
            float(candidate["end"]),
            _norm(candidate["display_name"]),
        )
        prior = unique.get(key)
        if (
            prior is None
            or (
                candidate["cue_kind"] == "chair-recognition"
                and prior["cue_kind"] != "chair-recognition"
            )
            or float(candidate["cue_start"]) < float(prior["cue_start"])
        ):
            unique[key] = candidate
    return list(unique.values())


def _timed_word_rows(words: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    rows = list(words.get("word_segments") or words.get("words") or [])
    for segment in words.get("segments") or []:
        if isinstance(segment, Mapping) and isinstance(segment.get("words"), list):
            rows.extend(segment["words"])
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        start = row.get("start", row.get("s"))
        end = row.get("end", row.get("e"))
        raw = str(row.get("word", row.get("w", row.get("text", "")))).strip()
        token = re.sub(r"[^\w'-]", "", raw.casefold())
        if isinstance(start, int | float) and isinstance(end, int | float) and token:
            yield {"start": float(start), "end": float(end), "raw": raw, "token": token}


def _sequence_at(rows: list[dict[str, Any]], index: int, sequence: tuple[str, ...]) -> bool:
    return [row["token"] for row in rows[index : index + len(sequence)]] == list(sequence)


def _name_after(
    rows: list[dict[str, Any]], start: int, known: Mapping[str, str]
) -> tuple[str | None, int]:
    for title in _ANNOUNCEMENT_TITLES:
        if _sequence_at(rows, start, title):
            start += len(title)
            break
    for length in range(min(4, len(rows) - start), 0, -1):
        candidate = rows[start : start + length]
        tokens = [row["token"] for row in candidate]
        if any(token in _NAME_STOP_WORDS for token in tokens):
            continue
        normalized = " ".join(tokens)
        if normalized in known:
            return known[normalized], start + length - 1
    collected: list[str] = []
    end_index = start - 1
    for index in range(start, min(len(rows), start + 4)):
        row = rows[index]
        token = row["token"]
        if token in _NAME_STOP_WORDS:
            break
        collected.append(row["raw"].strip(" ,:;.!?"))
        end_index = index
        if row["raw"].rstrip().endswith((",", ";", ":", ".", "!", "?")):
            break
    name = " ".join(part for part in collected if part).strip()
    return (name if name and any(char.isalpha() for char in name) else None), end_index


def _reference_candidate(
    rows: list[dict[str, Any]],
    turns: list[Mapping[str, Any]],
    cue_index: int,
    name_end_index: int,
    name: str,
    cue_kind: str,
) -> dict[str, Any] | None:
    cue_start = float(rows[cue_index]["start"])
    cue_end = float(rows[name_end_index]["end"])
    source = next(
        (
            turn
            for turn in turns
            if isinstance(turn.get("start"), int | float)
            and isinstance(turn.get("end"), int | float)
            and float(turn["start"]) <= cue_start
            and float(turn["end"]) >= cue_end
        ),
        None,
    )
    source_end = float(source["end"]) if source else cue_end
    eligible = [
        turn
        for turn in turns
        if isinstance(turn.get("start"), int | float)
        and isinstance(turn.get("end"), int | float)
        and float(turn["start"]) >= source_end - 0.15
        and float(turn["start"]) <= source_end + 20.0
        and not turn.get("overlap")
        and isinstance(turn.get("embedding"), list)
        and turn.get("embedding")
        and (source is None or turn.get("cluster") != source.get("cluster") or turn is not source)
    ]
    if not eligible:
        return None
    target = min(eligible, key=lambda turn: float(turn["start"]))
    cue_text = " ".join(row["raw"] for row in rows[cue_index : name_end_index + 1])
    return {
        "kind": "chair-reference",
        "cue_kind": cue_kind,
        "cue_text": cue_text,
        "cue_start": cue_start,
        "cue_end": cue_end,
        "display_name": name,
        "start": float(target["start"]),
        "end": float(target["end"]),
        "cluster": target.get("cluster"),
        "transcript_text_hash": target.get("transcript_text_hash"),
    }


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


def _person_by_name(
    registry: dict[str, Any], name: str, *, body_key_value: str
) -> tuple[str, dict[str, Any]] | None:
    wanted = _norm(name)
    for ident, person in (registry.get("people") or {}).items():
        if not isinstance(person, dict) or person.get("body_key") != body_key_value:
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
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            seen[_norm(name)] = name
    for item in votes:
        for vote in item.get("votes", []) if isinstance(item, Mapping) else []:
            if isinstance(vote, Mapping) and vote.get("member"):
                name = str(vote["member"]).strip()
                seen.setdefault(_norm(name), name)
    for name in seen.values():
        scoped_body_key = body_key(city_slug, body)
        found = _person_by_name(registry, name, body_key_value=scoped_body_key)
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
    embedding_recipe: str,
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
            if isinstance(row, Mapping) and row.get("embedding_recipe") == embedding_recipe
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
    "chair_reference_candidates",
    "calibration_cell",
    "empty_registry",
    "load_registry",
    "load_turn_evidence",
    "observe_attendance",
    "profile_matches",
    "pilot_selected",
    "public_turn",
    "qualified_profile",
    "quote_attribution",
    "reference_candidate_id",
    "refresh_membership_status",
    "save_registry",
    "save_evaluation",
    "save_turn_evidence",
    "shadow_candidate_id",
    "speaker_id",
    "valid_speaker_id",
]
