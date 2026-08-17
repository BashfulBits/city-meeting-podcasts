"""Unit tests for comment-preserving feed YAML insertion."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from citypods.feed_yaml_edit import (
    add_body_any,
    add_body_include,
    assert_only_addition,
)

FEEDS = Path(__file__).resolve().parents[1] / "config" / "feeds"

WITH_COMMENTS = """\
slug: example-tx-council
city: example-tx
provider: granicus
source:
  # The provider publishes two view ids for this body.
  feed_urls: ['https://a.example/rss', 'https://b.example/rss']
  body: "City Council"
  body_any:
    - "Work Session"  # legacy label, kept for the archive
  body_includes:
    - provider_guid: "1"
      body: "Special Joint Meeting"
podcast_title: "Example: Council"
podcast_author: "City of Example, TX"
podcast_email: ""
podcast_description: "Meetings."
"""

NO_SELECTORS = """\
slug: example-tx-board
city: example-tx
provider: granicus
source:
  feed_url: https://a.example/rss
podcast_title: "Example: Board"
podcast_author: "City of Example, TX"
podcast_email: ""
podcast_description: "Meetings."
"""


def test_add_body_any_appends_and_preserves_comments():
    out = add_body_any(WITH_COMMENTS, "Special Meeting")
    assert yaml.safe_load(out)["source"]["body_any"] == [
        "Work Session",
        "Special Meeting",
    ]
    assert "# The provider publishes two view ids for this body." in out
    assert '- "Work Session"  # legacy label, kept for the archive' in out
    # The flow-style sequence is a formatting choice safe_dump would destroy.
    assert "feed_urls: ['https://a.example/rss', 'https://b.example/rss']" in out


def test_add_body_any_creates_the_key_when_absent():
    out = add_body_any(NO_SELECTORS, "Planning Commission")
    assert yaml.safe_load(out)["source"]["body_any"] == ["Planning Commission"]
    assert yaml.safe_load(out)["source"]["feed_url"] == "https://a.example/rss"


def test_add_body_include_appends_to_existing_list():
    out = add_body_include(WITH_COMMENTS, "2", "Joint Luncheon")
    assert yaml.safe_load(out)["source"]["body_includes"] == [
        {"provider_guid": "1", "body": "Special Joint Meeting"},
        {"provider_guid": "2", "body": "Joint Luncheon"},
    ]


def test_add_body_include_creates_the_key_when_absent():
    out = add_body_include(NO_SELECTORS, "9", "One Off")
    assert yaml.safe_load(out)["source"]["body_includes"] == [
        {"provider_guid": "9", "body": "One Off"}
    ]


@pytest.mark.parametrize(
    ("edit", "key", "replacement"),
    [
        (add_body_any, "body_any", 'body_any: ["Work Session"]'),
        (add_body_include, "body_includes", "body_includes: []"),
    ],
)
def test_inline_selector_keys_are_rejected(edit, key, replacement):
    text = WITH_COMMENTS.replace(f"{key}:\n", f"{replacement}\n")
    with pytest.raises(ValueError, match=rf"{key!r} is written inline"):
        if edit is add_body_any:
            edit(text, "Special Meeting")
        else:
            edit(text, "2", "Joint Luncheon")


def test_edits_touch_only_the_intended_lines():
    out = add_body_any(WITH_COMMENTS, "Special Meeting")
    added = set(out.splitlines()) - set(WITH_COMMENTS.splitlines())
    assert added == {'    - "Special Meeting"'}


@pytest.mark.parametrize(
    "value",
    ['Quote " inside', "Back \\ slash", "Colon: value", "Ünïcode Böard", "Trailing  spaces  "],
)
def test_special_characters_round_trip(value):
    out = add_body_any(NO_SELECTORS, value)
    assert yaml.safe_load(out)["source"]["body_any"] == [value]


def test_assert_only_addition_rejects_a_no_op():
    with pytest.raises(ValueError):
        assert_only_addition(WITH_COMMENTS, WITH_COMMENTS, ("source", "body_any"), "X")


def test_assert_only_addition_rejects_an_unrelated_change():
    tampered = add_body_any(WITH_COMMENTS, "X").replace("granicus", "swagit")
    with pytest.raises(ValueError):
        assert_only_addition(WITH_COMMENTS, tampered, ("source", "body_any"), "X")


def test_missing_source_block_is_rejected():
    with pytest.raises(ValueError):
        add_body_any("slug: x\nprovider: granicus\n", "Anything")


@pytest.mark.parametrize(
    "path", sorted(p for p in FEEDS.glob("*.yml") if not p.name.startswith("_"))
)
def test_every_real_feed_survives_both_edits(path):
    """The editor has to cope with every shape actually committed to config/feeds."""
    before = path.read_text(encoding="utf-8")

    after_any = add_body_any(before, "ZZ Probe Body")
    assert_only_addition(before, after_any, ("source", "body_any"), "ZZ Probe Body")

    after_inc = add_body_include(before, "zz-probe-guid", "ZZ Probe Include")
    assert_only_addition(
        before,
        after_inc,
        ("source", "body_includes"),
        {"provider_guid": "zz-probe-guid", "body": "ZZ Probe Include"},
    )
