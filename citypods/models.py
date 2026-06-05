"""Normalized data models shared across providers and the rest of the pipeline.

Downstream code (feed building, site rendering, artwork, audio extraction) depends
only on these models and never on a concrete provider's wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from citypods.timeline import SourceMedia, Timeline


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
    """A single meeting recording, normalized across providers.

    ``media_kind`` describes ``video_url``:
      - ``"direct"``: a progressive MP4 usable as a podcast enclosure as-is (Granicus).
      - ``"hls"``: a (typically tokenized, expiring) HLS manifest that CANNOT be an
        enclosure; it must be materialized to a hosted M4A by the media pipeline, which
        then sets ``hosted_audio_url`` (CivicPlus / CivicMedia).
    """

    guid: str  # provider-native id (volatile across provider migrations)
    title: str
    published: datetime
    video_url: str
    description: str = ""
    audio_url: str | None = None  # falls back to video_url (audio/mp4) when None
    duration: int | None = None  # seconds
    media_kind: str = "direct"  # "direct" | "hls"
    hosted_audio_url: str | None = None  # set by the materialization pipeline
    body: str | None = None  # committee/meeting body, e.g. "City Council" (for per-body feeds)

    # --- stable identity + persisted derived artifacts (the EpisodeRecord, see records.py) ---
    # uid is a provider-independent identity (author+body+date) used as the RSS <guid> so that
    # provider migrations (Granicus<->Swagit) don't re-download a subscriber's back catalog.
    uid: str | None = None
    # Content-addressed audio: the key embeds the audio_spec_hash, so the object/URL changes
    # only when the audio *bytes* would change (codec/bitrate/chapters), enabling cache-bust,
    # rollback, and orphan detection. audio_spec_hash records the recipe the file was made with.
    audio_key: str | None = None
    audio_spec_hash: str | None = None
    # Enrichment artifacts populated by later stages (transcript/summary/chapters/links).
    links: dict = field(default_factory=dict)  # {"agenda": url, "canonical_video": url, ...}
    chapters: list = field(default_factory=list)  # [{"start": secs, "title": str}, ...]
    summary: str = ""

    # --- content-addressed transcript artifact (INFRA-8, #149) ---------------------
    # Replaces the old external-URL transcript_url field. Provider transcript links
    # that the ChaptersStage scrapes are still stored in ep.links["transcript"];
    # TranscriptStage fetches, stores, and remaps them into these hosted-artifact fields.
    transcript_key: str | None = None  # storage object key
    transcript_hosted_url: str | None = None  # public CDN URL
    transcript_spec_hash: str | None = None  # invalidation hash (source + version)
    transcript_format: str | None = None  # "vtt" | "srt" | "json" | "txt"
    # Time basis of cue timestamps: "source:s0" (from provider) or "served" (after remap).
    transcript_basis: str = "source:s0"
    # True = timestamps are present and correct against the enclosure; False = untimed
    # (plain text, PDF, or not yet remapped) — rendered as notes-only, never mis-aligned.
    transcript_synced: bool = False
    # Materialization backoff: when audio re-hosting fails (e.g. a Swagit ``/download`` that
    # redirects to a keyless S3 URL with no usable page media), the count of consecutive failed
    # attempts and the ISO8601 time of the last one are persisted so the media pipeline backs
    # off exponentially instead of re-trying — and burning the run budget — every run. Reset to
    # ``(0, None)`` on a successful host.
    materialize_attempts: int = 0
    materialize_last_attempt: str | None = None
    # Why the last attempt failed, for monitoring (feed-health audit + resource report): a
    # ``citypods.providers.base`` media category (``"deferred"`` = recoverable once a pending
    # feature ships, e.g. multi-segment Swagit concat #122; ``"dead"`` = no usable media) or
    # ``"error"`` for a transient/uncategorized failure. ``None`` once hosted successfully.
    materialize_error: str | None = None
    # Byte size of the hosted audio object, captured at put_file time (issue #124). Drives
    # exact per-feed and per-city GB totals in the status dashboard without a storage round-trip.
    # Older records carry ``None`` until re-hosted; the dashboard shows "~estimated" in that case.
    audio_bytes: int | None = None

    # --- v2 schema fields (INFRA-2, #143) -----------------------------------------------
    # SourceMedia registry: one entry per source file contributing to this episode's audio.
    # Empty list = single un-registered source (identity path; populated by TimelineStage).
    sources: list[SourceMedia] = field(default_factory=list)
    # Edit Decision List: None means identity (no manipulation). Set by TimelineStage.
    timeline: Timeline | None = None
    # Basis of ep.chapters: "source:s0" (provider time, before remap) or "served" (after).
    chapters_basis: str = "source:s0"
    # Surgical re-encode nonce (§4): when non-empty it's mixed into audio_spec_hash so only
    # stamped episodes get a new key and re-encode. Empty string = no effect on the hash.
    audio_rebuild: str = ""
    # ISO8601 timestamp recorded at encode time so rebuild-audio --encoded-after/before can
    # select a precise window without touching unaffected episodes. None for pre-v2 records.
    audio_encode_time: str | None = None
    # Served audio duration in seconds (may differ from source duration after trim/concat).
    # None for pre-v2 records; set by the encoder on each successful encode.
    audio_duration_served: float | None = None

    def resolved_audio_url(self) -> str:
        return self.audio_url or self.video_url

    def needs_materialization(self) -> bool:
        """True when no playable enclosure exists without re-hosting audio."""
        return self.media_kind == "hls" and not self.hosted_audio_url


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
    # Slug of the config/cities/<slug>.yml entity that supplied
    # city_website / meetings_url / state / colors.
    city_entity: str | None = None
    state: str | None = None
    city_website: str | None = None
    # Canonical meetings/agenda-portal URL, shown on every episode so listeners can reach the
    # city's own ground-truth site. Falls back to ``city_website`` when unset.
    meetings_url: str | None = None
    podcast_language: str = "en-us"
    podcast_category: str = "Government"
    max_episodes: int = 50
    extract_audio: bool = False
    # Re-host audio for ALL direct (Granicus) sources, not just those with extract_audio set.
    # Equivalent to setting extract_audio on every direct feed; use as a site-level knob to flip
    # host_frac → 1.0 (doc 03) without editing every feed YAML.
    host_all_audio: bool = False
    # body substrings to skip when generating per-board feeds
    body_exclude: list[str] = field(default_factory=list)
    # 1-2 brand hex colors for cover art (e.g. ["#0B5", "#fff"]); empty -> derived from name
    colors: list[str] = field(default_factory=list)
    # Former slugs this feed used to live at. Each gets a permanent redirect (an
    # itunes:new-feed-url stub feed + an HTML redirect page) so subscribers don't have to
    # re-subscribe if a feed must move. Slugs should otherwise never change.
    aliases: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)
