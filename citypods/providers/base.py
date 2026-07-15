"""The provider contract every video-hosting platform implements."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

import requests

from citypods.models import ChangeToken, Episode


class ProviderError(Exception):
    """Raised when a provider cannot fetch or parse a city's source."""


def is_transient_provider_error(exc: BaseException) -> bool:
    """Return whether a provider error carries a retryable requests transport cause."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (requests.ConnectionError, requests.Timeout)):
            return True
        current = current.__cause__ or current.__context__
    return False


# Stable categories for a materialization failure, recorded on the episode for monitoring
# (feed-health audit + resource report). ``DEFERRED`` = recoverable once a pending feature ships
# (e.g. multi-segment Swagit concat, issue #122); ``DEAD`` = no usable media exists at all.
MEDIA_DEFERRED = "deferred"
MEDIA_DEAD = "dead"


class MediaUnavailable(ProviderError):
    """``resolve_media_url`` cannot produce a usable audio source for this episode.

    ``code`` categorizes why (``MEDIA_DEFERRED`` / ``MEDIA_DEAD``) so the pipeline can persist it
    and the audit/report can distinguish "recoverable later" from "permanently dead". Other
    ``ProviderError``s (network/HTTP) are treated as transient (uncategorized)."""

    def __init__(self, message: str, *, code: Literal["deferred", "dead"]):
        super().__init__(message)
        self.code = code


@runtime_checkable
class MeetingProvider(Protocol):
    """Adapter for one platform (Granicus, CivicPlus, ...).

    Implementations are registered in ``citypods.providers`` and selected by the
    ``provider:`` key in a city's YAML. They translate platform-specific responses
    into the normalized :class:`~citypods.models.Episode` model.

    Capability declarations (INFRA-6, #147)
    -----------------------------------------
    ``capabilities`` is a ``frozenset[str]`` of feature tokens the provider supports.
    Downstream code gates optional behaviour on membership rather than isinstance checks:

    - ``"deeplink"`` — :meth:`video_deeplink` returns a non-None, time-anchored player URL
      that a human can click or a bot can download from. Features that deep-link back to the
      source video (newsletter, soundbites, per-meeting pages) check this before calling.

    Providers that cannot produce a time-anchored URL set ``capabilities = frozenset()`` and
    return ``None`` from :meth:`video_deeplink`; callers fall back to the plain ``watch_url``.
    """

    name: str
    capabilities: frozenset[str]

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
        """Fetch and parse the full episode list for a city.

        For providers whose media is direct (Granicus), ``Episode.video_url`` is a usable
        enclosure URL. For providers whose media must be materialized (CivicPlus/HLS),
        ``video_url`` holds a stable *reference* (e.g. the watch-page URL) and the real,
        often-expiring source URL is produced lazily by :meth:`resolve_media_url`.
        """
        ...

    def resolve_media_url(self, episode: Episode, source: dict) -> str:
        """Return the actual source media URL to hand to ffmpeg.

        Called by the materialization pipeline immediately before download, only for
        episodes being hosted this run (keeps expiring tokens fresh). The default returns
        the already-usable ``episode.video_url`` (correct for direct-media providers).
        """
        ...

    def video_deeplink(self, ref: str, t_seconds: float) -> str | None:
        """Return a player URL that opens the video at ``t_seconds`` in, or ``None``.

        ``ref`` is the ``SourceMedia.ref`` — the stable, non-expiring handle for a
        source (watch-page URL, clip id, etc.).  Only produces a URL when
        ``"deeplink" in self.capabilities``; returns ``None`` otherwise.

        Callers: always gate on ``"deeplink" in provider.capabilities`` before
        calling, and treat ``None`` as "no time-anchored link available — fall back
        to ``watch_url``."
        """
        ...


@runtime_checkable
class AgendaSource(Protocol):
    """Minimum shape that may enrich a primary episode with official links."""

    body: str
    published: datetime
    links: dict
    uid: str | None
