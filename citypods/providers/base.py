"""The provider contract every video-hosting platform implements."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

import requests

from citypods.models import ChangeToken, Episode

# Standard transient HTTP status codes: rate limits (429), timeouts (408), early data (425),
# standard server errors (500, 502, 503, 504), and Cloudflare origin/edge errors (520..527).
_TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset(
    {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 525, 526, 527}
)
_STATUS_CODE_RE = re.compile(
    r"(?:returned|status(?:_code)?|HTTP|code)\s*[:=]?\s*(\d{3})\b",
    re.IGNORECASE,
)


def _is_transient_status_code(code: int | None) -> bool:
    if code is None:
        return False
    return code in _TRANSIENT_HTTP_STATUSES or 500 <= code <= 599


class ProviderError(Exception):
    """Raised when a provider cannot fetch or parse a city's source."""

    def __init__(self, *args: object, status_code: int | None = None) -> None:
        super().__init__(*args)
        self.status_code = status_code


def is_transient_provider_error(exc: BaseException) -> bool:
    """Return whether a provider error carries a retryable requests transport or HTTP cause."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (requests.ConnectionError, requests.Timeout)):
            return True
        status = getattr(current, "status_code", None)
        if isinstance(status, int) and _is_transient_status_code(status):
            return True
        response = getattr(current, "response", None)
        resp_status = getattr(response, "status_code", None)
        if isinstance(resp_status, int) and _is_transient_status_code(resp_status):
            return True
        match = _STATUS_CODE_RE.search(str(current))
        if match:
            try:
                parsed_status = int(match.group(1))
                if _is_transient_status_code(parsed_status):
                    return True
            except ValueError:
                pass
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
