"""Feed validation: generated feeds must be structurally valid podcast RSS."""

from __future__ import annotations

import pytest

from citypods.config import load_city_configs, load_site_config
from citypods.feeds import build_rss
from citypods.providers.granicus import parse_feed
from citypods.validate import validate_feed
from tests.conftest import ROOT, SNAPSHOT_BASE_URL, fixture_bytes, recorded_slugs


def test_validator_flags_broken_feed():
    assert validate_feed(b"<rss><channel></channel></rss>")  # missing required elements
    assert validate_feed(b"not xml")


def _cities():
    site_config = load_site_config(ROOT / "site_config.yml")
    return {c.slug: c for c in load_city_configs(ROOT / "cities", site_config.get("defaults", {}))}


@pytest.mark.parametrize("slug", recorded_slugs())
@pytest.mark.parametrize("kind", ["audio", "video"])
def test_generated_feeds_valid(slug, kind):
    city = _cities()[slug]
    episodes = parse_feed(fixture_bytes("granicus", slug))
    xml = build_rss(city, episodes, kind, SNAPSHOT_BASE_URL)
    errors = validate_feed(xml)
    assert not errors, f"{slug} {kind} feed invalid: {errors}"
