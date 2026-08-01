"""Live contracts for known upstream chapter-index absences (GH#1078).

These pin the distinction established during the issue investigation: the selected Granicus
clips publish an explicit empty index, while the selected legacy Swagit pages must be fetched
through the same authenticated Worker-aware request path used by production before their lack of
``playerControl`` chapter markers is trusted.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from citypods.models import Episode
from citypods.providers.granicus import GranicusProvider
from citypods.providers.swagit import SwagitProvider

pytestmark = pytest.mark.live


def _episode(*, provider_guid: str, canonical_video: str, video_url: str) -> Episode:
    return Episode(
        guid=provider_guid,
        title="Chapter contract sample",
        published=datetime(2026, 1, 1, tzinfo=UTC),
        video_url=video_url,
        links={"canonical_video": canonical_video},
    )


@pytest.mark.parametrize("clip_id", (1885, 1772, 323))
def test_granicus_known_empty_indices_remain_empty(clip_id):
    """Denton County's known-empty JSON indexes must not look like a parser regression."""
    canonical = f"https://dentoncounty.granicus.com/MediaPlayer.php?view_id=26&clip_id={clip_id}"
    episode = _episode(
        provider_guid=canonical,
        canonical_video=canonical,
        video_url=(
            f"https://dentoncounty.granicus.com/DownloadFile.php?view_id=26&clip_id={clip_id}"
        ),
    )

    chapters, transcript = GranicusProvider().fetch_chapters(episode, {})

    assert chapters == []
    assert transcript is None


@pytest.mark.parametrize("video_id", (47686, 47660, 47637))
def test_swagit_known_empty_pages_remain_without_markers(video_id):
    """Use the production Worker fallback; direct local requests to these pages return 403."""
    canonical = f"https://austintx.new.swagit.com/videos/{video_id}"
    episode = _episode(
        provider_guid=str(video_id),
        canonical_video=canonical,
        video_url=f"{canonical}/download",
    )

    chapters, _transcript = SwagitProvider().fetch_chapters(episode, {})

    assert chapters == []
