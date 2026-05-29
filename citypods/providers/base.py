"""The provider contract every video-hosting platform implements."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from citypods.models import ChangeToken, Episode


class ProviderError(Exception):
    """Raised when a provider cannot fetch or parse a city's source."""


@runtime_checkable
class MeetingProvider(Protocol):
    """Adapter for one platform (Granicus, CivicPlus, ...).

    Implementations are registered in ``citypods.providers`` and selected by the
    ``provider:`` key in a city's YAML. They translate platform-specific responses
    into the normalized :class:`~citypods.models.Episode` model.
    """

    name: str

    def validate(self, source: dict) -> None:
        """Raise ``ValueError`` if ``source`` is missing required keys."""
        ...

    def detect_change(self, source: dict) -> ChangeToken | None:
        """Cheaply probe whether the source changed.

        Returns a token to compare against the cached one, or ``None`` if this
        provider cannot do cheap change detection (caller will always fetch).
        """
        ...

    def fetch_episodes(self, source: dict) -> list[Episode]:
        """Fetch and parse the full episode list for a city."""
        ...
