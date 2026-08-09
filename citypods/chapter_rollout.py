"""Pure rollout controls for generated chapter shadow and overlay experiments.

This module does not publish chapters and is intentionally independent of the episode pipeline.
It gives the eventual hydration stage one auditable policy shape: disabled by default, explicitly
bounded by provider/body/duration, and unable to enter overlay mode unless a separate shadow report
has passed its declared gates.  Keeping the decision pure makes it safe to exercise in a scheduled
monitoring job before generated records exist in production.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

RolloutMode = Literal["disabled", "shadow", "overlay"]


@dataclass(frozen=True)
class ChapterRolloutPolicy:
    """Bounded generated-chapter exposure policy; defaults to no publication."""

    mode: RolloutMode = "disabled"
    providers: tuple[str, ...] = ()
    bodies: tuple[str, ...] = ()
    max_duration_seconds: float | None = None
    max_episodes_per_run: int | None = None

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object] | None) -> ChapterRolloutPolicy:
        value = raw if isinstance(raw, Mapping) else {}
        mode = str(value.get("mode") or "disabled")
        if mode not in {"disabled", "shadow", "overlay"}:
            raise ValueError("chapter rollout mode must be disabled, shadow, or overlay")

        def names(key: str) -> tuple[str, ...]:
            values = value.get(key, ())
            if isinstance(values, str):
                values = (values,)
            if not isinstance(values, (list, tuple, set, frozenset)):
                raise ValueError(f"chapter rollout {key} must be a list of strings")
            result = tuple(
                sorted({str(item).strip().casefold() for item in values if str(item).strip()})
            )
            return result

        duration = value.get("max_duration_seconds")
        if duration is not None:
            try:
                duration = float(duration)
            except (TypeError, ValueError) as exc:
                raise ValueError("chapter rollout max_duration_seconds must be positive") from exc
            if duration <= 0:
                raise ValueError("chapter rollout max_duration_seconds must be positive")
        limit = value.get("max_episodes_per_run")
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise ValueError("chapter rollout max_episodes_per_run must be a positive integer")
        return cls(
            mode=mode,  # type: ignore[arg-type]
            providers=names("providers"),
            bodies=names("bodies"),
            max_duration_seconds=duration,
            max_episodes_per_run=limit,
        )

    def allows_episode(
        self, *, provider: str, body: str | None = None, duration_seconds: float | None = None
    ) -> bool:
        """Return whether the episode is inside the configured bounded cohort."""
        if self.mode == "disabled":
            return False
        if self.providers and provider.casefold() not in self.providers:
            return False
        if self.bodies and (body or "").casefold() not in self.bodies:
            return False
        if self.max_duration_seconds is not None and (
            duration_seconds is None or duration_seconds > self.max_duration_seconds
        ):
            return False
        return True

    def effective_mode(self, *, shadow_gate_status: str, eligible: bool) -> RolloutMode:
        """Downgrade overlay to shadow unless the independent report gate passes."""
        if not eligible or self.mode == "disabled":
            return "disabled"
        if self.mode == "shadow":
            return "shadow"
        return "overlay" if shadow_gate_status == "pass" else "shadow"
