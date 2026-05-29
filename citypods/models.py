"""Normalized data models shared across providers and the rest of the pipeline.

Downstream code (feed building, site rendering, artwork, audio extraction) depends
only on these models and never on a concrete provider's wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ChangeToken:
    """An opaque marker a provider can compare to decide if a city changed.

    For feed-based providers this is the HTTP ETag / Last-Modified. Scraper-based
    providers may use a content hash. Equality is all the caller relies on.
    """

    etag: str | None = None
    last_modified: str | None = None
    content_hash: str | None = None

    def is_empty(self) -> bool:
        return not (self.etag or self.last_modified or self.content_hash)


@dataclass
class Episode:
    """A single meeting recording, normalized across providers."""

    guid: str
    title: str
    published: datetime
    video_url: str
    description: str = ""
    audio_url: str | None = None  # falls back to video_url (audio/mp4) when None
    duration: int | None = None  # seconds

    def resolved_audio_url(self) -> str:
        return self.audio_url or self.video_url


@dataclass
class City:
    """A configured city, after merging site-level defaults."""

    slug: str
    provider: str
    source: dict
    podcast_title: str
    podcast_author: str
    podcast_email: str
    podcast_description: str
    state: str | None = None
    city_website: str | None = None
    podcast_language: str = "en-us"
    podcast_category: str = "Government"
    max_episodes: int = 50
    extract_audio: bool = False
    extra: dict = field(default_factory=dict)
