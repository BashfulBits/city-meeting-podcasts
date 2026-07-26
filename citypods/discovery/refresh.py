"""Conditional source refresh and normalized episode-input fingerprints."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from citypods.models import ChangeToken, Episode

REFRESH_STATE_NAME = "source_refresh.json"
_VOLATILE_QUERY_KEYS = frozenset(
    {
        "token",
        "tokenid",
        "signature",
        "sig",
        "expires",
        "expiry",
        "auth",
        "jwt",
        "policy",
        "key-pair-id",
        "nonce",
    }
)
_VOLATILE_QUERY_PREFIXES = ("x-amz-", "hdn")


def _iso(value: datetime | None = None) -> str:
    return (value or datetime.now(UTC)).astimezone(UTC).isoformat()


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def token_to_dict(token: ChangeToken | None) -> dict[str, str]:
    if token is None:
        return {}
    return {
        key: value
        for key, value in {
            "etag": token.etag,
            "last_modified": token.last_modified,
            "content_hash": token.content_hash,
        }.items()
        if value
    }


def token_from_dict(value: object) -> ChangeToken | None:
    if not isinstance(value, dict):
        return None
    token = ChangeToken(
        etag=value.get("etag") if isinstance(value.get("etag"), str) else None,
        last_modified=(
            value.get("last_modified") if isinstance(value.get("last_modified"), str) else None
        ),
        content_hash=(
            value.get("content_hash") if isinstance(value.get("content_hash"), str) else None
        ),
    )
    return None if token.is_empty() else token


def tokens_equal(left: ChangeToken | None, right: ChangeToken | None) -> bool:
    """Missing validators never prove a source unchanged."""
    return left is not None and right is not None and not left.is_empty() and left == right


def _canonical_media_ref(value: str | None) -> str | None:
    if not value:
        return value
    parts = urlsplit(value)
    query = [
        (key, val)
        for key, val in parse_qsl(parts.query, keep_blank_values=True)
        if (
            key.casefold() not in _VOLATILE_QUERY_KEYS
            and not key.casefold().startswith(_VOLATILE_QUERY_PREFIXES)
        )
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def episode_input_fingerprint(ep: Episode) -> str:
    """Hash trusted provider inputs that can affect downstream work.

    Provider GUIDs and signed media URLs are deliberately excluded: UIDs survive provider
    migrations and expiring URLs must not make unchanged media dirty. Derived/LLM fields are
    excluded from this trust-boundary input.
    """
    published = ep.published.replace(tzinfo=UTC) if ep.published.tzinfo is None else ep.published
    payload = {
        "uid": ep.uid,
        "title": ep.title,
        "published": published.astimezone(UTC).isoformat(),
        "description": ep.description,
        "video_url": _canonical_media_ref(ep.video_url),
        "audio_url": _canonical_media_ref(ep.audio_url),
        "duration": ep.duration,
        "source_duration_seconds": ep.source_duration_seconds,
        "media_kind": ep.media_kind,
        "body": ep.body,
        "links": sorted((ep.links or {}).items()),
    }
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def episode_input_fingerprints(episodes: list[Episode]) -> dict[str, str]:
    return {
        ep.uid: episode_input_fingerprint(ep)
        for ep in episodes
        if isinstance(ep.uid, str) and ep.uid
    }


def normalized_content_digest(fingerprints: dict[str, str]) -> str:
    blob = json.dumps(sorted(fingerprints.items()), separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def dirty_uids(previous: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    return {
        uid: ("new" if uid not in previous else "input_changed")
        for uid, fingerprint in current.items()
        if previous.get(uid) != fingerprint
    }


def load_refresh_state(state_dir: Path) -> dict[str, dict[str, Any]]:
    path = Path(state_dir) / REFRESH_STATE_NAME
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def save_refresh_state(state_dir: Path, state: dict[str, dict[str, Any]]) -> None:
    path = Path(state_dir) / REFRESH_STATE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def refresh_due(
    metadata: dict[str, Any] | None,
    *,
    ttl_hours: float = 0.0,
    full_refresh_days: float = 7.0,
    now: datetime | None = None,
) -> bool:
    """Return whether a source should be probed/fetched.

    Zero TTL is the compatibility-safe default: validator-less sources are fetched and
    fingerprint-compared each run. Operators can raise it to bound polling while the full-refresh
    ceiling remains an independent stale-catalog backstop.
    """
    if not metadata:
        return True
    clock = now or datetime.now(UTC)
    # ``last_full_refresh`` is deliberately separate from validator probes: a source that returns
    # the same ETag forever still receives a bounded full reconciliation.
    last_success = _parse_time(metadata.get("last_full_refresh")) or _parse_time(
        metadata.get("last_success")
    )
    if last_success is None:
        return True
    if full_refresh_days > 0 and clock - last_success >= timedelta(days=full_refresh_days):
        return True
    if ttl_hours <= 0:
        return True
    next_poll = _parse_time(metadata.get("next_poll_at"))
    return next_poll is None or clock >= next_poll


def next_poll_at(*, now: datetime | None = None, ttl_hours: float = 0.0) -> str | None:
    if ttl_hours <= 0:
        return None
    return _iso((now or datetime.now(UTC)) + timedelta(hours=ttl_hours))
