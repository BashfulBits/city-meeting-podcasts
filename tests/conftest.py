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


def fixture_bytes(provider: str, slug: str) -> bytes:
    return (FIXTURE_DIR / provider / f"{slug}.xml").read_bytes()


def recorded_slugs(provider: str = "granicus") -> list[str]:
    return sorted(p.stem for p in (FIXTURE_DIR / provider).glob("*.xml"))


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
