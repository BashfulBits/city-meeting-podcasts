"""Unit tests for RSS feed building."""

from __future__ import annotations

from xml.etree import ElementTree as ET

import pytest

from citypods.feeds import build_rss


def _items(xml: str):
    return ET.fromstring(xml).find("channel").findall("item")


def test_audio_uses_audio_mime(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "audio", "https://x")
    enc = _items(xml)[0].find("enclosure")
    assert enc.get("type") == "audio/mp4"


def test_video_uses_video_mime(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    enc = _items(xml)[0].find("enclosure")
    assert enc.get("type") == "video/mp4"


def test_enclosure_length_zero(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    assert _items(xml)[0].find("enclosure").get("length") == "0"


def test_episodes_ordered_newest_first(sample_city, sample_episodes):
    # Pass them in reverse; output must be sorted by published desc.
    xml = build_rss(sample_city, list(reversed(sample_episodes)), "video", "https://x")
    titles = [i.find("title").text for i in _items(xml)]
    assert titles[0].startswith("Regular")


def test_truncates_to_max_episodes(sample_city, sample_episodes):
    sample_city.max_episodes = 1
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    assert len(_items(xml)) == 1


def test_xml_escaping(sample_city, sample_episodes):
    xml = build_rss(sample_city, sample_episodes, "video", "https://x")
    assert "&lt;Meeting&gt;" in xml and "&amp;" in xml
    # And it still parses.
    ET.fromstring(xml)


def test_invalid_kind_raises(sample_city, sample_episodes):
    with pytest.raises(ValueError):
        build_rss(sample_city, sample_episodes, "transcript", "https://x")
