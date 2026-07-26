"""Fail-closed civic-platform discovery for R12.

Search results and LLM output are evidence inputs, never trusted configuration.  This package
keeps retrieval, classification, verification, issue rendering, and proposal assembly separate so
the workflow can expose findings without ever writing a city config directly.
"""

from citypods.discovery.models import (
    Classification,
    DiscoveryMode,
    DiscoveryRequest,
    DiscoveryResult,
    SearchResult,
)
from citypods.discovery.refresh import (
    dirty_uids,
    episode_input_fingerprint,
    episode_input_fingerprints,
)
from citypods.discovery.search import TavilyClient, TavilySearchError
from citypods.discovery.verify import verify_discovery


def __getattr__(name: str):
    """Load the Pydantic-backed classifier only for callers that request it."""
    if name == "classify":
        from citypods.discovery.classify import classify

        return classify
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Classification",
    "DiscoveryMode",
    "DiscoveryRequest",
    "DiscoveryResult",
    "SearchResult",
    "TavilyClient",
    "TavilySearchError",
    "classify",
    "verify_discovery",
    "dirty_uids",
    "episode_input_fingerprint",
    "episode_input_fingerprints",
]
