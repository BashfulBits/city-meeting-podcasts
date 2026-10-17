"""Load config/provider_catalog_decisions.yml (see that file for the semantics)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Any

import yaml

DECISIONS_PATH = Path(__file__).resolve().parents[2] / "config" / "provider_catalog_decisions.yml"


@dataclass(frozen=True)
class Decisions:
    ignored: tuple[dict[str, Any], ...] = ()
    acknowledged: tuple[dict[str, Any], ...] = ()

    def is_ignored(self, provider: str, model: str, today: date) -> bool:
        for entry in self.ignored:
            if entry.get("provider") != provider or entry.get("model") != model:
                continue
            revisit = entry.get("revisit_after")
            if revisit and date.fromisoformat(str(revisit)) <= today:
                continue
            return True
        return False

    def acknowledgement(self, provider: str, model: str, verdict: str) -> dict[str, Any] | None:
        for entry in self.acknowledged:
            if (
                entry.get("provider") == provider
                and entry.get("verdict") == verdict
                and fnmatchcase(model, str(entry.get("model_glob", "")))
            ):
                return entry
        return None


def _validate(section: str, entries: Any, required: tuple[str, ...]) -> tuple[dict, ...]:
    if entries in (None, []):
        return ()
    if not isinstance(entries, list):
        raise ValueError(f"{section} must be a list")
    for i, entry in enumerate(entries):
        missing = [k for k in required if not isinstance(entry, dict) or not entry.get(k)]
        if missing:
            raise ValueError(f"{section}[{i}] is missing {', '.join(missing)}")
    return tuple(entries)


def load_decisions(path: Path = DECISIONS_PATH) -> Decisions:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    raw = raw or {}
    return Decisions(
        ignored=_validate(
            "ignored", raw.get("ignored"), ("provider", "model", "reason", "decided_on")
        ),
        acknowledged=_validate(
            "acknowledged",
            raw.get("acknowledged"),
            ("provider", "model_glob", "verdict", "reason", "decided_on"),
        ),
    )
