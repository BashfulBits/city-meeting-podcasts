"""Structural validation for generated podcast RSS.

Not a full iTunes spec validator — it checks well-formedness plus the channel and
item elements podcast players actually require, so CI can fail fast on a broken feed.
"""

from __future__ import annotations

from pathlib import Path

import defusedxml.ElementTree as ET
from defusedxml.common import DefusedXmlException

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
_REDIRECT_TAG = f"{{{ITUNES}}}new-feed-url"
_EMPTY_ERROR = "channel has no <item> entries"

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
    except (ET.ParseError, DefusedXmlException) as exc:
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
        errors.append(_EMPTY_ERROR)

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


def validate_build(
    output_dir: Path,
    known_empty: frozenset[str] | set[str] = frozenset(),
) -> tuple[list[str], list[str]]:
    """Validate every ``*.xml`` feed under *output_dir*.

    Returns ``(fatals, warnings)`` where each item is a string of the form
    ``"<slug>/feed.xml: <problem>"``.  Callers should exit non-zero when
    *fatals* is non-empty.

    ``known_empty`` is a set of city slugs whose feeds are expected to contain
    no items (e.g. a brand-new city whose audio backfill has not run yet).
    Empty feeds for those slugs are demoted to warnings; all others are fatal.

    Redirect feeds (containing ``<itunes:new-feed-url>``) are intentionally
    item-free and structurally incomplete — they are skipped entirely.
    """
    fatals: list[str] = []
    warnings: list[str] = []

    for xml_path in sorted(Path(output_dir).rglob("*.xml")):
        rel = str(xml_path.relative_to(output_dir))
        slug = xml_path.parent.name

        try:
            content = xml_path.read_bytes()
        except OSError as exc:
            fatals.append(f"{rel}: cannot read: {exc}")
            continue

        # Redirect feeds carry itunes:new-feed-url and intentionally omit items
        # and most channel elements — skip them.
        try:
            root = ET.fromstring(content)
            channel = root.find("channel")
            if channel is not None and channel.find(_REDIRECT_TAG) is not None:
                continue
        except (ET.ParseError, DefusedXmlException):
            pass  # fall through; validate_feed will report the parse error

        for problem in validate_feed(content):
            if problem == _EMPTY_ERROR and slug in known_empty:
                warnings.append(f"{rel}: {problem}")
            else:
                fatals.append(f"{rel}: {problem}")

    return fatals, warnings


def _pretty(tag: str) -> str:
    if tag.startswith(f"{{{ITUNES}}}"):
        return "itunes:" + tag.split("}", 1)[1]
    return tag
