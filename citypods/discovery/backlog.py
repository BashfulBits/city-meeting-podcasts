"""Canonical, reviewable unsupported-provider backlog for R12 research findings."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml


def load_pending_providers(path: str | Path) -> dict[str, Any]:
    raw = yaml.safe_load(Path(path).read_text()) if Path(path).exists() else {}
    if not isinstance(raw, dict):
        raise ValueError("pending-provider tracker must be a mapping")
    raw.setdefault("providers", {})
    if not isinstance(raw["providers"], dict):
        raise ValueError("pending-provider tracker providers must be a mapping")
    return raw


def assign_provider(
    tracker: dict[str, Any],
    *,
    provider_key: str,
    city_slug: str,
    origin_issue: int | None,
    evidence_url: str | None,
    checked_at: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Idempotently add/update one city without erasing the originating evidence."""
    updated = deepcopy(tracker)
    providers = updated.setdefault("providers", {})
    entry = providers.setdefault(
        provider_key,
        {
            "name": name or provider_key,
            "adapter_status": "research needed",
            "next_action": "verify platform scope",
            "cities": {},
        },
    )
    if name:
        entry["name"] = name
    cities = entry.setdefault("cities", {})
    city = cities.setdefault(city_slug, {})
    city.update(
        {
            "origin_issue": origin_issue,
            "evidence_url": evidence_url,
            "last_checked": checked_at,
        }
    )
    city.setdefault("discovered_at", checked_at)
    return updated


def dump_pending_providers(tracker: dict[str, Any]) -> str:
    return yaml.safe_dump(tracker, sort_keys=True)


def render_backlog_summary(tracker: dict[str, Any]) -> str:
    lines = [
        "## Unsupported civic-provider backlog",
        "",
        "| Provider | Cities pending | Status | Next action |",
        "|---|---:|---|---|",
    ]
    for key, entry in sorted(tracker.get("providers", {}).items()):
        if not isinstance(entry, dict):
            continue
        lines.append(
            f"| {entry.get('name', key)} | {len(entry.get('cities', {}))} | "
            f"{entry.get('adapter_status', 'research needed')} | {entry.get('next_action', '')} |"
        )
    return "\n".join(lines) + "\n"
