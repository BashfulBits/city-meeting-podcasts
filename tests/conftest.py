"""Shared test fixtures and helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from citypods.models import City, Episode

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"
GRANICUS_FIXTURES = FIXTURE_DIR / "granicus"

# Fixed base URL so generated feeds are byte-for-byte deterministic in snapshots.
SNAPSHOT_BASE_URL = "https://podcasts.example.gov"


def _fixture_file(provider: str, slug: str) -> Path:
    matches = sorted((FIXTURE_DIR / provider).glob(f"{slug}.*"))
    if not matches:
        raise FileNotFoundError(f"no fixture for {provider}/{slug}")
    return matches[0]


def fixture_bytes(provider: str, slug: str) -> bytes:
    return _fixture_file(provider, slug).read_bytes()


def recorded_slugs(provider: str = "granicus") -> list[str]:
    d = FIXTURE_DIR / provider
    if not d.exists():
        return []
    return sorted(p.stem for p in d.glob("*.*"))


# Deterministic CDN base used to simulate already-materialized CivicPlus audio in
# snapshot/validation tests (the real pipeline + ffmpeg are tested separately).
SIM_AUDIO_CDN = "https://cdn.example.gov/audio"


def _simulate_hosted(slug: str, eps):
    # A stable, slug-based fake URL just for deterministic snapshots (independent of the
    # real source-based audio key, which is exercised in test_media).
    import hashlib

    for e in eps:
        digest = hashlib.sha1(e.guid.encode()).hexdigest()[:16]
        e.hosted_audio_url = f"{SIM_AUDIO_CDN}/{slug}/{digest}.m4a"
    return eps


def episodes_for(provider: str, slug: str):
    """Parsed episodes ready for feed building.

    Providers whose audio is re-hosted (CivicPlus always; CivicClerk via extract_audio)
    get simulated hosted audio URLs so the audio feed renders deterministically.
    """
    from citypods.providers.civicclerk import parse_events
    from citypods.providers.civicplus import parse_civicmedia_feed
    from citypods.providers.granicus import parse_feed
    from citypods.providers.swagit import parse_list

    data = fixture_bytes(provider, slug)
    if provider == "granicus":
        return parse_feed(data)
    if provider == "civicplus":
        return _simulate_hosted(slug, parse_civicmedia_feed(data))
    if provider == "civicclerk":
        # Travis County uses category 26 + extract_audio (hosted M4A audio, direct MP4 video).
        return _simulate_hosted(slug, parse_events(data, category_id=26))
    if provider == "swagit":
        # The recorded fixture is pre-trimmed to the City Council body.
        eps = parse_list(data, "https://dallastx.new.swagit.com")
        return _simulate_hosted(slug, eps)
    raise AssertionError(f"unknown provider {provider}")


def kinds_for(provider: str) -> tuple[str, ...]:
    # CivicPlus and Swagit are audio-only (ephemeral media re-hosted as audio).
    # Granicus/CivicClerk are direct-MP4 so they get both an audio and a video feed.
    return ("audio",) if provider in ("civicplus", "swagit") else ("audio", "video")


def all_fixture_cases() -> list[tuple[str, str, str]]:
    cases = []
    for provider in ("granicus", "civicplus", "civicclerk", "swagit"):
        for slug in recorded_slugs(provider):
            for kind in kinds_for(provider):
                cases.append((provider, slug, kind))
    return cases


@pytest.fixture
def sample_city() -> City:
    return City(
        slug="denton-tx",
        provider="granicus",
        source={"feed_url": "https://example.granicus.com/x"},
        podcast_title="Denton City Council",
        podcast_author="City of Denton, TX",
        podcast_email="clerk@denton.gov",
        podcast_description="Recordings of Denton City Council meetings.",
        state="TX",
    )


@pytest.fixture
def sample_episodes() -> list[Episode]:
    return [
        Episode(
            guid="clip-2",
            title="Regular <Meeting> & Work Session",
            published=datetime(2025, 5, 20, 18, 0, tzinfo=UTC),
            video_url="https://media.example.com/2.mp4",
            description="Second & latest.",
            duration=3723,
        ),
        Episode(
            guid="clip-1",
            title="Earlier Meeting",
            published=datetime(2025, 5, 13, 18, 0, tzinfo=UTC),
            video_url="https://media.example.com/1.mp4",
            description="First.",
        ),
    ]
