"""Stable judgment identities.

``subject_id`` must not move when an unrelated field changes. The existing review ``candidate_id``
includes the pre-labeler's decision, reason and input digest, so it changes whenever the
pre-labeler runs; it is never used here (review/53 PR3).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _evidence_key(candidate: Mapping[str, Any]) -> list[list[Any]]:
    rows = []
    for item in candidate.get("evidence") or []:
        if isinstance(item, Mapping):
            rows.append([item.get("where"), item.get("span"), item.get("t")])
    return sorted(rows, key=lambda row: json.dumps(row, default=str))


def tag_subject_id(episode_uid: str, candidate: Mapping[str, Any]) -> str:
    return "subj-" + _digest(
        {
            "task": "tag",
            "episode_uid": episode_uid,
            "chapter_id": candidate.get("chapter_id"),
            "tag": candidate.get("id"),
            "source_kind": candidate.get("source_kind", "llm"),
            "producer": candidate.get("provider_model"),
            "rule_version": candidate.get("rule_version"),
            "evidence": _evidence_key(candidate),
        }
    )


def moment_subject_id(episode_uid: str, candidate: Mapping[str, Any]) -> str:
    return "subj-" + _digest(
        {
            "task": "moment",
            "episode_uid": episode_uid,
            "quote": candidate.get("quote"),
            "quote_start": candidate.get("quote_start"),
            "quote_end": candidate.get("quote_end"),
            "producer": candidate.get("provider_model"),
            "prompt_version": candidate.get("prompt_version"),
        }
    )
