"""Timeline/audio integrity repair helpers.

The repair block is intentionally small and record-compatible.  Audit/diagnostic code writes
``integrity.timeline_audio`` entries; timeline, audio, and transcript stages consume the
``repair`` actions as targeted rebuild inputs without bumping global pipeline versions.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from citypods.models import Episode

TIMELINE_AUDIO = "timeline_audio"

REPAIR_TIMELINE_REPLAN = "timeline-replan"
REPAIR_AUDIO_REMATERIALIZE = "audio-rematerialize"
REPAIR_TRANSCRIPT_REGENERATE = "transcript-regenerate"

REPAIR_ACTIONS = frozenset(
    {
        REPAIR_TIMELINE_REPLAN,
        REPAIR_AUDIO_REMATERIALIZE,
        REPAIR_TRANSCRIPT_REGENERATE,
    }
)


def timeline_audio_integrity(ep: Episode) -> dict:
    block = ep.integrity.get(TIMELINE_AUDIO) if isinstance(ep.integrity, dict) else None
    return block if isinstance(block, dict) else {}


def timeline_audio_repair_actions(ep: Episode) -> frozenset[str]:
    repair = timeline_audio_integrity(ep).get("repair")
    if not isinstance(repair, list):
        return frozenset()
    return frozenset(str(action) for action in repair if str(action) in REPAIR_ACTIONS)


def needs_timeline_audio_repair(ep: Episode, action: str) -> bool:
    return action in timeline_audio_repair_actions(ep)


def timeline_audio_repair_token(ep: Episode, action: str) -> str:
    """Stable token folded into targeted rebuild recipes for one repair action."""
    if action not in timeline_audio_repair_actions(ep):
        return ""
    block = timeline_audio_integrity(ep)
    tokens = block.get("repair_tokens") if isinstance(block, dict) else None
    if isinstance(tokens, dict) and tokens.get(action):
        return str(tokens[action])
    seed = {
        "v": 1,
        "uid": ep.uid or ep.guid,
        "action": action,
        "status": block.get("status"),
        "checked_at": block.get("checked_at"),
        "timeline_duration": block.get("timeline_duration"),
        "stream_sample_duration": block.get("stream_sample_duration"),
        "decoded_duration": block.get("decoded_duration"),
    }
    blob = json.dumps(seed, separators=(",", ":"), sort_keys=True)
    return hashlib.sha1(blob.encode()).hexdigest()[:12]


def ensure_timeline_audio_repair_token(ep: Episode, action: str) -> str:
    token = timeline_audio_repair_token(ep, action)
    if not token:
        return ""
    block = dict(timeline_audio_integrity(ep))
    tokens = dict(block.get("repair_tokens") or {})
    if tokens.get(action) != token:
        tokens[action] = token
        block["repair_tokens"] = tokens
        set_timeline_audio_integrity(ep, block)
    return token


def set_timeline_audio_integrity(ep: Episode, block: dict | None) -> None:
    integrity = dict(ep.integrity or {})
    if block:
        current = dict(timeline_audio_integrity(ep))
        integrity[TIMELINE_AUDIO] = {**current, **block}
    else:
        integrity.pop(TIMELINE_AUDIO, None)
    ep.integrity = integrity


def build_timeline_audio_integrity(
    *,
    status: str,
    timeline_duration: float | None,
    container_duration: float | None,
    stream_sample_duration: float | None,
    repair: list[str] | None = None,
    source_duration_delta: float | None = None,
) -> dict:
    block = {
        "status": status,
        "checked_at": datetime.now(UTC).isoformat(),
        "timeline_duration": timeline_duration,
        "container_duration": container_duration,
        "stream_sample_duration": stream_sample_duration,
        "repair": sorted(repair or []),
    }
    if source_duration_delta is not None:
        block["source_duration_delta"] = source_duration_delta
    return {k: v for k, v in block.items() if v is not None and v != []}


def clear_resolved_timeline_audio_integrity(ep: Episode, status: str) -> bool:
    """Clear a prior repair flag after audit sees a healthy post-repair state."""
    if status != "ok" or not timeline_audio_integrity(ep):
        return False
    block = dict(timeline_audio_integrity(ep))
    if "repair" not in block:
        return False
    block.pop("repair", None)
    block.pop("repair_tokens", None)
    block["status"] = "ok"
    block["resolved_at"] = datetime.now(UTC).isoformat()
    set_timeline_audio_integrity(ep, block)
    return True
