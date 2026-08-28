"""Feed validation: generated feeds must be structurally valid podcast RSS."""

from __future__ import annotations

import pytest

from citypods.config import load_city_configs, load_site_config
from citypods.feeds import build_rss
from citypods.validate import validate_build, validate_feed
from tests.conftest import ROOT, SNAPSHOT_BASE_URL, all_fixture_cases, episodes_for

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"


def test_validator_flags_broken_feed():
    assert validate_feed(b"<rss><channel></channel></rss>")  # missing required elements
    assert validate_feed(b"not xml")


def _cities():
    site_config = load_site_config(ROOT / "config" / "site_config.yml")
    return {c.slug: c for c in load_city_configs(ROOT / "config", site_config.get("defaults", {}))}


@pytest.mark.parametrize("provider,slug,kind", all_fixture_cases())
def test_generated_feeds_valid(provider, slug, kind):
    city = _cities()[slug]
    episodes = episodes_for(provider, slug)
    xml = build_rss(city, episodes, kind, SNAPSHOT_BASE_URL)
    errors = validate_feed(xml)
    assert not errors, f"{slug} {kind} feed invalid: {errors}"


# ---------------------------------------------------------------------------
# validate_build tests
# ---------------------------------------------------------------------------


def _write_feed(path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _empty_valid_xml() -> bytes:
    """Well-formed RSS with required channel elements but no items (backfill-in-progress)."""
    return (
        f'<rss version="2.0" xmlns:itunes="{ITUNES}">'
        "<channel>"
        "<title>Test</title>"
        "<link>http://example.com</link>"
        "<description>desc</description>"
        "<itunes:author>Author</itunes:author>"
        '<itunes:category text="Society &amp; Culture"/>'
        '<itunes:image href="http://example.com/art.jpg"/>'
        "</channel></rss>"
    ).encode()


def _corrupt_xml() -> bytes:
    return b"<rss><channel><this is broken"


def _redirect_xml() -> bytes:
    return (
        f'<rss version="2.0" xmlns:itunes="{ITUNES}">'
        "<channel>"
        "<title>Moved</title>"
        "<link>http://example.com/new</link>"
        "<description>moved</description>"
        "<itunes:new-feed-url>http://example.com/new/feed.xml</itunes:new-feed-url>"
        "</channel></rss>"
    ).encode()


def test_validate_build_passes_on_real_feeds(tmp_path):
    """Real fixture feeds (with items) pass validation cleanly."""
    cities = _cities()
    for provider, slug, kind in all_fixture_cases():
        city = cities[slug]
        episodes = episodes_for(provider, slug)
        xml = build_rss(city, episodes, kind, SNAPSHOT_BASE_URL)
        feed_dir = tmp_path / slug
        feed_dir.mkdir(exist_ok=True)
        (feed_dir / f"{kind}_feed.xml").write_text(xml)

    fatals, warnings = validate_build(tmp_path)
    assert not fatals, f"Unexpected fatals: {fatals}"
    assert not warnings


def test_validate_build_fatal_on_corrupt_xml(tmp_path):
    _write_feed(tmp_path / "city-a" / "audio_feed.xml", _corrupt_xml())
    fatals, warnings = validate_build(tmp_path)
    assert any("city-a/audio_feed.xml" in f for f in fatals)
    assert not warnings


def test_validate_build_fatal_on_unexpected_empty(tmp_path):
    """An empty feed whose slug is NOT in known_empty is a fatal error."""
    _write_feed(tmp_path / "city-b" / "audio_feed.xml", _empty_valid_xml())
    fatals, warnings = validate_build(tmp_path)
    assert any("city-b/audio_feed.xml" in f for f in fatals)
    assert not warnings


def test_validate_build_warn_on_known_empty(tmp_path):
    """An empty feed whose slug IS in known_empty is a warning, not fatal."""
    _write_feed(tmp_path / "city-c" / "audio_feed.xml", _empty_valid_xml())
    fatals, warnings = validate_build(tmp_path, known_empty={"city-c"})
    assert not fatals
    assert any("city-c/audio_feed.xml" in w for w in warnings)


def test_validate_build_skips_redirect_feeds(tmp_path):
    """Redirect feeds (itunes:new-feed-url) are intentionally item-free — skip them."""
    _write_feed(tmp_path / "old-slug" / "audio_feed.xml", _redirect_xml())
    fatals, warnings = validate_build(tmp_path)
    assert not fatals
    assert not warnings


def test_validate_build_mixed(tmp_path):
    """Known-empty warning doesn't suppress a corrupt feed in the same run."""
    _write_feed(tmp_path / "ok-city" / "audio_feed.xml", _empty_valid_xml())
    _write_feed(tmp_path / "bad-city" / "audio_feed.xml", _corrupt_xml())
    fatals, warnings = validate_build(tmp_path, known_empty={"ok-city"})
    assert any("bad-city" in f for f in fatals)
    assert any("ok-city" in w for w in warnings)
    assert not any("ok-city" in f for f in fatals)


def test_xxe_vulnerability_prevented():
    # A simple XXE payload
    xxe_xml = """<?xml version="1.0" encoding="ISO-8859-1"?>
<!DOCTYPE foo [
  <!ELEMENT foo ANY >
  <!ENTITY xxe SYSTEM "file:///etc/passwd" >]><foo>&xxe;</foo>
"""

    # defusedxml should return an error list with the exception in it
    errors = validate_feed(xxe_xml)
    assert len(errors) > 0
    assert "not well-formed XML" in errors[0]


def test_validate_build_rejects_xxe(tmp_path):
    """The build-wide validator must reject XML entity declarations too."""
    xxe_xml = b"""<?xml version="1.0"?>
<!DOCTYPE foo [
  <!ENTITY xxe SYSTEM "file:///etc/passwd">
]>
<foo>&xxe;</foo>
"""
    _write_feed(tmp_path / "malicious-city" / "audio_feed.xml", xxe_xml)

    fatals, warnings = validate_build(tmp_path)

    assert not warnings
    assert any("malicious-city/audio_feed.xml" in fatal for fatal in fatals)
    assert any("not well-formed XML" in fatal for fatal in fatals)
