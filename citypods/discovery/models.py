"""Typed, serializable inputs and outputs for civic-platform discovery."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

DiscoveryMode = Literal["auxiliary", "new-city"]

# Keys are deliberately provider names where an adapter exists. ``primegov`` is normalized to
# the OneMeeting/PrimeGov adapter by :mod:`citypods.discovery.verify`.
KNOWN_PLATFORMS = frozenset(
    {
        "granicus",
        "swagit",
        "civicplus",
        "civicclerk",
        "civicengage",
        "legistar",
        "onemeeting",
        "primegov",
        "agenda-pe",
        "agendaquick",
        "boarddocs",
        "civicweb",
        "escribe",
        "municode",
        "sire",
        "clerkbase",
        "boardbook",
        "simbli",
        "catalis",
        "streamline",
    }
)


@dataclass(frozen=True)
class SearchResult:
    """One Tavily result, reduced to the fields safe to pass to an LLM."""

    url: str
    title: str = ""
    content: str = ""
    score: float | None = None


@dataclass(frozen=True)
class DiscoveryRequest:
    """One city discovery request. Form values are hints, never trusted facts."""

    mode: DiscoveryMode
    city_name: str
    state: str
    city_slug: str
    known_provider: str | None = None
    city_website: str | None = None
    meeting_url_hint: str | None = None
    provider_hint: str | None = None
    notes: str | None = None
    issue_number: int | None = None


@dataclass(frozen=True)
class Classification:
    """The constrained structured result produced by ``classify-civic-platforms``."""

    video_platform: str | None
    agenda_platform: str | None
    candidate_urls: tuple[str, ...]
    bodies_mentioned: tuple[str, ...]
    # Provider-specific source mappings. Every URL in either mapping must occur verbatim in
    # Tavily evidence; non-URL values are constrained by the provider schema in classify.py.
    video_source: dict[str, Any] | None = None
    agenda_source: dict[str, Any] | None = None
    confidence: Literal["low", "medium", "high"] = "low"
    reasoning: str = ""


@dataclass(frozen=True)
class Verification:
    """Mandatory verification output. ``applyable`` is never LLM-controlled."""

    platform: str | None
    signature_url: str | None
    signature_verified: bool
    provider_verified: bool
    sample_media_url: str | None
    source: dict[str, Any] | None
    reason: str = ""

    @property
    def applyable(self) -> bool:
        return bool(
            self.signature_verified
            and self.provider_verified
            and self.sample_media_url
            and self.source is not None
        )


@dataclass(frozen=True)
class DiscoveryResult:
    """Complete evidence package rendered to an issue comment or rolling digest."""

    request: DiscoveryRequest
    search_results: tuple[SearchResult, ...]
    classification: Classification
    verification: Verification
    proposed_yaml: str | None = None
    city_website_url: str | None = None
    meeting_listing_url: str | None = None
    research_only: bool = False
    evidence_created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """A JSON-safe artifact for Actions, issue-state reconciliation, and tests."""
        return asdict(self)
