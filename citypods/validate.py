"""Structural validation for generated podcast RSS.

Not a full iTunes spec validator — it checks well-formedness plus the channel and
item elements podcast players actually require, so CI can fail fast on a broken feed.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"

REQUIRED_CHANNEL = [
    "title",
    "link",
    "description",
    f"{{{ITUNES}}}author",
    f"{{{ITUNES}}}category",
    f"{{{ITUNES}}}image",
]


def validate_feed(xml: str | bytes) -> list[str]:
    """Return a list of problems; empty means the feed is valid."""
    errors: list[str] = []
    try:
        root = ET.fromstring(xml.encode() if isinstance(xml, str) else xml)
    except ET.ParseError as exc:
        return [f"not well-formed XML: {exc}"]

    if root.tag != "rss":
        errors.append(f"root element is <{root.tag}>, expected <rss>")
    channel = root.find("channel")
    if channel is None:
        return errors + ["missing <channel>"]

    for tag in REQUIRED_CHANNEL:
        if channel.find(tag) is None:
            errors.append(f"channel missing <{_pretty(tag)}>")

    items = channel.findall("item")
    if not items:
        errors.append("channel has no <item> entries")

    for i, item in enumerate(items):
        label = f"item[{i}]"
        if item.find("title") is None:
            errors.append(f"{label} missing <title>")
        if item.find("guid") is None:
            errors.append(f"{label} missing <guid>")
        if item.find("pubDate") is None:
            errors.append(f"{label} missing <pubDate>")
        enc = item.find("enclosure")
        if enc is None:
            errors.append(f"{label} missing <enclosure>")
        else:
            if not enc.get("url"):
                errors.append(f"{label} enclosure missing url")
            if not enc.get("type"):
                errors.append(f"{label} enclosure missing type")

    return errors


def _pretty(tag: str) -> str:
    if tag.startswith(f"{{{ITUNES}}}"):
        return "itunes:" + tag.split("}", 1)[1]
    return tag
