"""Canonical duration helpers with compatibility reads for legacy record fields."""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Literal

from citypods.models import Episode

DurationSource = Literal["served", "source", "unknown"]


def _positive_seconds(value: object) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def episode_source_duration_seconds(ep: Episode) -> float | None:
    return _positive_seconds(ep.source_duration_seconds) or _positive_seconds(ep.duration)


def episode_served_duration_seconds(ep: Episode) -> float | None:
    return _positive_seconds(ep.served_duration_seconds) or _positive_seconds(
        ep.audio_duration_served
    )


def episode_duration_hours(ep: Episode) -> tuple[float, DurationSource]:
    served = episode_served_duration_seconds(ep)
    if served is not None:
        return served / 3600.0, "served"
    source = episode_source_duration_seconds(ep)
    if source is not None:
        return source / 3600.0, "source"
    return 0.0, "unknown"


def record_source_duration_seconds(rec: MutableMapping[str, object]) -> float | None:
    return _positive_seconds(rec.get("source_duration_seconds")) or _positive_seconds(
        rec.get("duration")
    )


def record_served_duration_seconds(rec: MutableMapping[str, object]) -> float | None:
    audio = rec.get("audio")
    legacy = audio.get("duration_served") if isinstance(audio, MutableMapping) else None
    return _positive_seconds(rec.get("served_duration_seconds")) or _positive_seconds(legacy)


def record_duration_hours(rec: MutableMapping[str, object]) -> tuple[float, DurationSource]:
    served = record_served_duration_seconds(rec)
    if served is not None:
        return served / 3600.0, "served"
    source = record_source_duration_seconds(rec)
    if source is not None:
        return source / 3600.0, "source"
    return 0.0, "unknown"


def set_source_duration_seconds(
    ep_or_rec: Episode | MutableMapping[str, object],
    value: float | None,
    *,
    basis: str | None = None,
    evidence: str | None = None,
) -> None:
    seconds = _positive_seconds(value)
    if isinstance(ep_or_rec, Episode):
        ep_or_rec.source_duration_seconds = seconds
        ep_or_rec.duration = None if seconds is None else int(round(seconds))
        return
    ep_or_rec["source_duration_seconds"] = seconds
    ep_or_rec["duration"] = None if seconds is None else int(round(seconds))
    _unused = (basis, evidence)


def set_served_duration_seconds(
    ep_or_rec: Episode | MutableMapping[str, object],
    value: float | None,
    *,
    basis: str | None = None,
    evidence: str | None = None,
) -> None:
    seconds = _positive_seconds(value)
    if isinstance(ep_or_rec, Episode):
        ep_or_rec.served_duration_seconds = seconds
        ep_or_rec.audio_duration_served = seconds
        return
    ep_or_rec["served_duration_seconds"] = seconds
    audio = ep_or_rec.get("audio")
    if not isinstance(audio, MutableMapping):
        audio = {}
        ep_or_rec["audio"] = audio
    audio["duration_served"] = seconds
    _unused = (basis, evidence)
