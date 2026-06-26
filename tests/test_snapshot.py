"""Regression snapshot tests.

The primary safety net: generated RSS for each recorded city is compared byte-for-byte
against a committed golden file. If a code change alters delivered feed entries, the diff
fails CI. Most real changes are usability or new cities, which must NOT perturb existing
feeds.

To regenerate goldens after an intentional change:

    SNAPSHOT_UPDATE=1 pytest tests/test_snapshot.py
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from citypods.config import load_city_configs, load_site_config
from citypods.feeds import build_rss
from citypods.providers import get_provider
from citypods.records import assign_uids
from citypods.stages import LinksStage, StageContext
from tests.conftest import ROOT, SNAPSHOT_BASE_URL, all_fixture_cases, episodes_for

SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"


def _enrich_links(provider: str, city, episodes):
    """Run the feed-only ``links`` enrichment the real build runs, so the snapshot pins the
    rendered resource links (provider agenda/video + the LinksStage-injected ``canonical_video``
    default and the per-feed ``meetings`` page) rather than just the raw provider parse. The
    stage is pure/offline for these providers (no network, no storage), so it stays deterministic.
    """
    ctx = StageContext(storage=None, ffmpeg=None, max_kbps=96, dry_run=True, stop=None)
    LinksStage().process(get_provider(provider), city, episodes, ctx)


def _city(slug: str):
    site_config = load_site_config(ROOT / "config" / "site_config.yml")
    cities = load_city_configs(ROOT / "config", site_config.get("defaults", {}))
    for c in cities:
        if c.slug == slug:
            return c
    raise AssertionError(f"no city config for recorded fixture {slug!r}")


@pytest.mark.parametrize("provider,slug,kind", all_fixture_cases())
def test_feed_snapshot(provider, slug, kind):
    city = _city(slug)
    episodes = episodes_for(provider, slug)
    assign_uids(city, episodes)  # mirror the real build so the snapshot pins the stable guid
    _enrich_links(provider, city, episodes)  # mirror the build's feed-only link enrichment
    generated = build_rss(city, episodes, kind, SNAPSHOT_BASE_URL)

    snapshot = SNAPSHOT_DIR / f"{slug}_{kind}.xml"
    if os.environ.get("SNAPSHOT_UPDATE"):
        SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(generated, encoding="utf-8")
        pytest.skip(f"updated snapshot {snapshot.name}")

    assert snapshot.exists(), (
        f"missing snapshot {snapshot.name}; run SNAPSHOT_UPDATE=1 pytest to create it"
    )
    assert generated == snapshot.read_text(encoding="utf-8"), (
        f"{snapshot.name} changed; if intentional, regenerate with SNAPSHOT_UPDATE=1"
    )
