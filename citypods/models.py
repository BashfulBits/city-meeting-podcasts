"""Normalized data models shared across providers and the rest of the pipeline.

Downstream code (feed building, site rendering, artwork, audio extraction) depends
only on these models and never on a concrete provider's wire format.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from citypods.timeline import SourceMedia, Timeline

if TYPE_CHECKING:
    from citypods.availability import MediaAvailability


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
    # Legacy source-duration scalar; canonical = source_duration_seconds.
    duration: int | None = None  # seconds
    source_duration_seconds: float | None = None
    served_duration_seconds: float | None = None
    media_kind: Literal["direct", "hls"] = "direct"
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
    # Source-time provider chapter markers retained durably so a later timeline change can
    # reproject them onto a new served clock. Empty for synthetic served-only planners like concat.
    source_chapters: list = field(default_factory=list)
    chapters: list = field(default_factory=list)  # [{"start": secs, "title": str}, ...]
    summary: str = ""

    # --- content-addressed transcript artifact (INFRA-8, #149) ---------------------
    # Active podcast transcript artifact.  ASR / provider-aligned served-time transcripts live
    # here and drive <podcast:transcript>.  Provider-supplied source documents are retained
    # separately in ``provider_transcript`` below so ASR/alignment can supersede the podcast
    # tag without losing the city-provided download or its re-alignment/rollback history.
    transcript_key: str | None = None  # storage object key
    transcript_hosted_url: str | None = None  # public CDN URL
    transcript_spec_hash: str | None = None  # invalidation hash (source + version)
    transcript_format: str | None = None  # "vtt" | "srt" | "json" | "txt"
    # Time basis of cue timestamps: "source:s0" (from provider) or "served" (after remap).
    transcript_basis: str = "source:s0"
    # True = timestamps are present and correct against the enclosure; False = untimed
    # (plain text, PDF, or not yet remapped) — rendered as notes-only, never mis-aligned.
    transcript_synced: bool = False
    # Word-level JSON sidecar (H12): clean segment VTT above is served to apps;
    # this holds per-word timings for server-side search / clips / diarization.
    transcript_words_key: str | None = None  # storage object key for the word JSON
    transcript_words_url: str | None = None  # public CDN URL for the word JSON
    # ASR pipeline version that produced this transcript (None = provider-supplied or
    # pre-versioning). Drives version-aware re-transcription: an ASR transcript from an
    # older version is re-done; provider transcripts are never invalidated by a bump.
    transcript_pipeline_version: str | None = None
    # Consecutive local-inference timeouts and the ISO8601 timestamp of the latest one. Persisted
    # per episode so a pathological recording backs off across scheduled ASR runs instead of
    # repeatedly consuming the full item timeout. Reset after successful local inference.
    transcript_timeout_attempts: int = 0
    transcript_timeout_last_attempt: str | None = None

    # City/provider supplied transcript document registry (PR1 schema).  Shape is intentionally
    # record-compatible and implementation-neutral until the follow-up stages consume it:
    # {
    #   "known_good": {url, key, spec_hash, format, basis, synced, confidence, checked_at, ...},
    #   "candidate": {same shape, queued for provider-transcript-align review},
    #   "history": [{prior known_good/candidate entries, newest first}],
    # }
    # The active transcript fields above remain the feed-facing transcript.  This block backs the
    # separate "Original city-provided transcript" download link and prevents ASR/alignment from
    # erasing provider source material.
    provider_transcript: dict = field(default_factory=dict)

    # Speaker/diarization artifact. PT-PR6 starts with provider-transcript-derived speaker turns;
    # later ML diarization writes the same independent block without disturbing transcript text.
    speakers_key: str | None = None
    speakers_url: str | None = None
    speakers_spec_hash: str | None = None
    speakers_format: str | None = None
    speakers_synced: bool = False
    speakers_confidence: float | None = None
    speakers_pipeline_version: str | None = None
    speakers_error: str | None = None
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
    # Audio spec hash that was attempted when ``materialize_error`` was recorded. This lets targeted
    # repair specs get one fresh attempt without disabling backoff for the same broken recipe.
    materialize_error_spec_hash: str | None = None
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
    # Transient, per-run TimelineStage defer marker. Not persisted; it prevents AudioStage from
    # encoding stale/weak-basis timelines when planning could not safely complete this run.
    timeline_defer_reason: str = ""
    # Basis of ep.chapters: "source:s0" (provider time, before remap) or "served" (after).
    chapters_basis: str = "source:s0"
    # Surgical re-encode nonce (§4): when non-empty it's mixed into audio_spec_hash so only
    # stamped episodes get a new key and re-encode. Empty string = no effect on the hash.
    audio_rebuild: str = ""
    # ISO8601 timestamp recorded at encode time so rebuild-audio --encoded-after/before can
    # select a precise window without touching unaffected episodes. None for pre-v2 records.
    audio_encode_time: str | None = None
    # The *probed* duration in seconds of the actual hosted enclosure (the served/hosted clock,
    # review/20). Differs from the source ``duration`` after trim/concat, and is kept distinct from
    # the EDL/cue clock (``timeline.edl_duration``): a render that disagrees with the EDL must stay
    # visible to the audit rather than being reconciled into this field. None for pre-v2 records;
    # set from the post-encode ffprobe (or a reused artifact's recorded duration) on each success.
    # Legacy served-duration mirror; canonical = served_duration_seconds.
    audio_duration_served: float | None = None

    # Audit-owned repair/diagnostic metadata.  ``integrity.timeline_audio`` is consumed by
    # timeline/audio/transcript stages as targeted rebuild input; other lanes preserve it.
    integrity: dict = field(default_factory=dict)

    # --- durable media-availability projection (H16 PR3, GH#353) -----------------------
    # Versioned verdict on whether this meeting's source media is usable: available / suspected
    # or confirmed empty / missing / invalid / recovered, plus operator overrides. Withheld
    # verdicts keep bad/empty enclosures out of feeds and the media-affecting stages while
    # metadata stages continue (review/12 PR3, review/13). None = never classified (e.g. direct
    # enclosures we don't re-host, or pre-PR3 records). See citypods/availability.py.
    media_availability: MediaAvailability | None = None

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
    # body substrings to skip when generating per-board feeds
    body_exclude: list[str] = field(default_factory=list)
    # 1-2 brand hex colors for cover art (e.g. ["#0B5", "#fff"]); empty -> derived from name
    colors: list[str] = field(default_factory=list)
    # Former slugs this feed used to live at. Each gets a permanent redirect (an
    # itunes:new-feed-url stub feed + an HTML redirect page) so subscribers don't have to
    # re-subscribe if a feed must move. Slugs should otherwise never change.
    aliases: list[str] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    # --- ASR config (#110) --------------------------------------------------
    # These mirror the site_config defaults and can be overridden per feed.
    asr_enabled: bool = True
    asr_model: str = "large-v3-turbo"  # faster-whisper model name; overridden at runtime
    # by ASR_MODEL_PATH env var (scripts/prepare_whisper.py cascade)
    asr_compute_type: str = "int8"  # int8 = fastest/most mem-efficient on CPU
    asr_language: str = "en"  # Whisper language hint; empty string = auto-detect
    asr_workers: int = 1  # episode-level parallelism (1 model instance per worker)
    asr_beam_size: int = 5  # Whisper beam size; reduce to 1 for ~2× speed
    # Temporarily disabled in production while Phase H moves alignment into its own
    # resource lane; untimed provider transcripts remain notes-only when false.
    asr_alignment_enabled: bool = False
