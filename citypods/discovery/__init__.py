"""Fail-closed civic-platform discovery for R12.

Search results and LLM output are evidence inputs, never trusted configuration.  This package
keeps retrieval, classification, verification, issue rendering, and proposal assembly separate so
the workflow can expose findings without ever writing a city config directly.
"""

from citypods.discovery.classify import classify
from citypods.discovery.models import (
    Classification,
    DiscoveryMode,
    DiscoveryRequest,
    DiscoveryResult,
    SearchResult,
)
from citypods.discovery.search import TavilyClient, TavilySearchError
from citypods.discovery.verify import verify_discovery

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
]
