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
PILOT_SCOPE_VERSION = "2"
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
_SELF_INTRO_CUES: tuple[tuple[str, ...], ...] = (
    ("my", "name", "is"),
    ("this", "is"),
    ("i'm",),
    ("i", "am"),
)
# Common single-word civic staff titles, matched loosely (any token, not an exact phrase) since
# real title wording varies too much ("Assistant Planner", "Senior Engineer", "City Attorney")
# to enumerate as fixed sequences the way `_ANNOUNCEMENT_TITLES` does for elected titles.
_STAFF_TITLE_WORDS = frozenset(
    {
        "administrator",
        "analyst",
        "assistant",
        "attorney",
        "chief",
        "clerk",
        "coordinator",
        "director",
        "engineer",
        "manager",
        "officer",
        "planner",
        "secretary",
        "specialist",
        "superintendent",
        "supervisor",
    }
)
_SELF_INTRODUCTION_WINDOW_SECONDS = 10.0
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


def _pilot_row_matches_city(row: Mapping[str, Any], city_slug: str) -> bool:
    """Return whether a pilot row matches the given city slug or feed slug."""
    target = str(row.get("city") or "").strip()
    if not target:
        return False
    if target == "*":
        return True
    if target == city_slug:
        return True
    # Accept feed slugs that start with entity prefix
    # (e.g. 'denton-tx-city-council' vs 'denton-tx').
    if city_slug.startswith(f"{target}-"):
        return True
    return False


def pilot_selected(config: Mapping[str, Any], city_slug: str, body: str | None) -> bool:
    """Return whether an opt-in R7 pilot explicitly includes this city/body pair.

    An empty allowlist is deliberately not interpreted as "all".  Public-name calibration is a
    sensitive rollout, so enabling the subsystem without selecting a pilot stays fail-closed.
    To explicitly allow all cities or bodies, use wildcards ('*') or allow_all_cities: true.
    """
    if config.get("allow_all_cities") is True:
        if config.get("allow_all_bodies") is True:
            return True
        for row in config.get("pilot_bodies") or []:
            if isinstance(row, Mapping) and _pilot_row_matches(row, body):
                return True
        if not config.get("pilot_bodies"):
            return True
    for row in config.get("pilot_bodies") or []:
        if not isinstance(row, Mapping):
            continue
        if not _pilot_row_matches_city(row, city_slug):
            continue
        if _pilot_row_matches(row, body):
            return True
    return False


def _pilot_row_matches(row: Mapping[str, Any], body: str | None) -> bool:
    """Match an explicit body selector against raw provider body labels."""
    exact = _norm(row.get("body"))
    if exact == "*" or row.get("all_bodies") is True:
        return True
    actual = _norm(body)
    if exact and actual == exact:
        return True
    prefixes = [_norm(row.get("body_prefix"))]
    prefixes.extend(_norm(value) for value in row.get("body_prefixes") or [])
    for prefix in prefixes:
        if not prefix:
            continue
        if prefix == "*":
            return True
        if not actual.startswith(prefix):
            continue
        suffix = actual[len(prefix) :].lstrip(" -:;,–—")
        if suffix.split(" ", 1)[0] in {"joint", "section"}:
            continue
        return True
    return False


def pilot_capture_context(
    config: Mapping[str, Any], city_slug: str, body: str | None
) -> str | None:
    """Return the explicit capture context for one selected pilot body.

    Calibration evidence is only portable within a capture setup.  A provider name is too broad:
    one body can move from a dais mix to an audience mic without changing providers.  Keep the
    context on the allowlist row so enabling a new setup necessarily creates a new cell.
    """
    for row in config.get("pilot_bodies") or []:
        if not isinstance(row, Mapping):
            continue
        if not _pilot_row_matches_city(row, city_slug) or not _pilot_row_matches(row, body):
            continue
        context = str(row.get("capture_context") or "").strip()
        if not context:
            if row.get("city") == "*" or row.get("body") == "*":
                return f"{city_slug}-audio-v1"
            raise ValueError("each R7 pilot body requires a non-empty capture_context")
        return context
    if config.get("allow_all_cities") is True:
        return f"{city_slug}-audio-v1"
    return None


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


def _name_then_title(
    rows: list[dict[str, Any]], start: int, known: Mapping[str, str]
) -> tuple[str | None, int] | None:
    """Match "<Name>, <staff title>" at `start` -- a self-introduction with no framing phrase
    (review/31 §C.3), distinct from `_name_after`'s "cue phrase, then a name" shape."""
    collected: list[str] = []
    end_index = start - 1
    saw_break = False
    for index in range(start, min(len(rows), start + 4)):
        row = rows[index]
        if row["token"] in _NAME_STOP_WORDS or row["token"] in _STAFF_TITLE_WORDS:
            break
        collected.append(row["raw"].strip(" ,:;.!?"))
        end_index = index
        if row["raw"].rstrip().endswith((",", ";", ":")):
            saw_break = True
            break
        if row["raw"].rstrip().endswith((".", "!", "?")):
            return None
    if not saw_break or not collected:
        return None
    name = " ".join(part for part in collected if part).strip()
    if not name or not any(char.isalpha() for char in name):
        return None
    # A known-attendee roster (when one parsed) corrects the raw ASR name text to the official
    # spelling below; without one (a new city, or unparseable minutes) this signal still fires
    # on the title check alone -- roster narrowing is a quality improvement, not a requirement.
    normalized = " ".join(row["token"] for row in rows[start : end_index + 1])
    title_index = next(
        (
            index
            for index in range(end_index + 1, min(len(rows), end_index + 5))
            if rows[index]["token"] in _STAFF_TITLE_WORDS
        ),
        None,
    )
    if title_index is None:
        return None
    return known.get(normalized, name), title_index


def self_introduction_candidates(
    words: Mapping[str, Any], turns: Iterable[Mapping[str, Any]], *, known_names: Iterable[str] = ()
) -> list[dict[str, Any]]:
    """Find a speaker naming themselves within the first ~10s of their own turn.

    A second automatic evidence signal alongside `chair_reference_candidates` (review/31 §C.3):
    staff presenters and public commenters frequently self-introduce at a podium ("MY NAME IS
    REZA...", "MATT BODINE, ASSISTANT PLANNER...") -- not universal, so this is one more
    candidate for human confirmation, exactly like `chair_reference_candidates`, never a direct
    assignment. The corroborated span is the turn itself (the speaker is naming *themselves*),
    unlike the chair-cue case, which corroborates the *next* turn after the cue.
    """
    rows = list(_timed_word_rows(words))
    turn_rows = [row for row in turns if isinstance(row, Mapping)]
    known = {_norm(name): str(name).strip() for name in known_names if str(name).strip()}
    matches: list[dict[str, Any]] = []
    for turn in turn_rows:
        start, end = turn.get("start"), turn.get("end")
        if (
            turn.get("overlap")
            or not isinstance(turn.get("embedding"), list)
            or not turn.get("embedding")
            or not isinstance(start, int | float)
            or not isinstance(end, int | float)
        ):
            continue
        window_end = min(float(end), float(start) + _SELF_INTRODUCTION_WINDOW_SECONDS)
        window = [row for row in rows if float(start) <= row["start"] < window_end]
        for index in range(len(window)):
            for cue in _SELF_INTRO_CUES:
                if not _sequence_at(window, index, cue):
                    continue
                name, name_end = _name_after(window, index + len(cue), known)
                if name:
                    matches.append(
                        _self_introduction_candidate(
                            window, turn, index, name_end, name, "self-stated"
                        )
                    )
            title_match = _name_then_title(window, index, known)
            if title_match:
                name, name_end = title_match
                matches.append(
                    _self_introduction_candidate(
                        window, turn, index, name_end, name, "name-then-title"
                    )
                )
    unique: dict[tuple, dict[str, Any]] = {}
    for candidate in matches:
        # A "name-then-title" match can also fire on a shorter trailing substring of the same
        # name anchored to the same title word (e.g. "Bodine," alone, inside "Matt Bodine,");
        # key on the title's own position rather than the extracted name so the earliest
        # (longest) name wins instead of both surviving as separate candidates.
        if candidate["cue_kind"] == "name-then-title":
            key = (
                float(candidate["start"]),
                float(candidate["end"]),
                "name-then-title",
                float(candidate["cue_end"]),
            )
        else:
            key = (
                float(candidate["start"]),
                float(candidate["end"]),
                _norm(candidate["display_name"]),
            )
        prior = unique.get(key)
        if prior is None or float(candidate["cue_start"]) < float(prior["cue_start"]):
            unique[key] = candidate
    return list(unique.values())


def _self_introduction_candidate(
    rows: list[dict[str, Any]],
    turn: Mapping[str, Any],
    cue_index: int,
    name_end_index: int,
    name: str,
    cue_kind: str,
) -> dict[str, Any]:
    cue_text = " ".join(row["raw"] for row in rows[cue_index : name_end_index + 1])
    return {
        "kind": "self-introduction",
        "cue_kind": cue_kind,
        "cue_text": cue_text,
        "cue_start": float(rows[cue_index]["start"]),
        "cue_end": float(rows[name_end_index]["end"]),
        "display_name": name,
        "start": float(turn["start"]),
        "end": float(turn["end"]),
        "cluster": turn.get("cluster"),
        "transcript_text_hash": turn.get("transcript_text_hash"),
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


def qualified_profile(person: Mapping[str, Any], *, embedding_recipe: str) -> bool:
    """A public auto-match needs two meetings under the active embedding recipe."""
    references = [
        row
        for row in person.get("references", [])
        if isinstance(row, Mapping) and row.get("embedding_recipe") == embedding_recipe
    ]
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
        if not qualified_profile(person, embedding_recipe=embedding_recipe):
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


def calibration_cell(
    city_slug: str,
    body: str | None,
    engine_recipe: str,
    *,
    capture_context: str,
) -> str:
    """Return a body/recipe/capture-scoped calibration cell."""
    if not str(capture_context).strip():
        raise ValueError("R7 calibration requires an explicit capture context")
    return "|".join((city_slug, _norm(body), engine_recipe, _norm(capture_context)))


def roster_person_ids(
    registry: Mapping[str, Any], roster: Iterable[Mapping[str, Any]]
) -> set[str] | None:
    """Return registry ids supported by a parseable official roster, or ``None``.

    ``None`` deliberately differs from an empty set: missing or malformed minutes must make no
    correction, while a valid roster containing no profile holder must remove a stale projection.
    """
    names = {
        _norm(item.get("name"))
        for item in roster
        if isinstance(item, Mapping) and str(item.get("name") or "").strip()
    }
    if not names:
        return None
    matches: set[str] = set()
    for ident, person in (registry.get("people") or {}).items():
        if not isinstance(person, Mapping):
            continue
        known = {
            _norm(person.get("display_name")),
            *(_norm(alias) for alias in person.get("aliases") or []),
        }
        if names & (known - {""}):
            matches.add(str(ident))
    return matches


def auto_publish_allowed(
    state: Mapping[str, Any], *, cell: str, engine: str, now: datetime | None = None
) -> bool:
    """Require the locked 30-day/30-review/95%-precision calibration policy."""
    if not any(
        isinstance(row, Mapping)
        and row.get("cell") == cell
        and row.get("selected_engine") == engine
        for row in state.get("benchmarks", [])
    ):
        return False
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
    confirmed: bool = False,
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
        "status": "confirmed" if publish and confirmed else "provisional" if publish else "shadow",
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
    "PILOT_SCOPE_VERSION",
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
    "pilot_capture_context",
    "public_turn",
    "qualified_profile",
    "roster_person_ids",
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
