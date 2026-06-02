"""Unit tests for config loading and validation."""

from __future__ import annotations

import textwrap

import pytest

from citypods.config import load_city_configs

DEFAULTS = {"podcast_language": "en-us", "podcast_category": "Government", "max_episodes": 50}

VALID = """\
slug: foo-tx
provider: granicus
source:
  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2
podcast_title: Foo Council
podcast_author: City of Foo
podcast_email: ""
podcast_description: Meetings.
state: TX
"""


def _write(dir_, name, body):
    """Write a feed YAML into the config dir's feeds/ subdir."""
    feeds = dir_ / "feeds"
    feeds.mkdir(exist_ok=True)
    (feeds / name).write_text(textwrap.dedent(body))


def _write_entity(dir_, name, body):
    """Write an entity YAML into the config dir's cities/ subdir."""
    entities = dir_ / "cities"
    entities.mkdir(exist_ok=True)
    (entities / name).write_text(textwrap.dedent(body))


def test_loads_valid_city(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID)
    cities = load_city_configs(tmp_path, DEFAULTS)
    assert len(cities) == 1
    c = cities[0]
    assert c.slug == "foo-tx"
    assert c.podcast_email == ""  # blank email allowed through
    assert c.max_episodes == 50  # inherited default


def test_blank_email_allowed_but_key_required(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID.replace('podcast_email: ""\n', ""))
    with pytest.raises(ValueError, match="podcast_email"):
        load_city_configs(tmp_path, DEFAULTS)


def test_missing_required_key_raises(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID.replace("podcast_title: Foo Council\n", ""))
    with pytest.raises(ValueError, match="podcast_title"):
        load_city_configs(tmp_path, DEFAULTS)


def test_unknown_provider_raises(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID.replace("provider: granicus", "provider: bogus"))
    with pytest.raises(Exception, match="provider"):
        load_city_configs(tmp_path, DEFAULTS)


def test_provider_validates_source(tmp_path):
    body = VALID.replace(
        "source:\n  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n",
        "source:\n  wrong_key: oops\n",
    )
    _write(tmp_path, "foo-tx.yml", body)
    with pytest.raises(ValueError, match="feed_url"):
        load_city_configs(tmp_path, DEFAULTS)


def test_duplicate_slug_raises(tmp_path):
    _write(tmp_path, "a.yml", VALID)
    _write(tmp_path, "b.yml", VALID)
    with pytest.raises(ValueError, match="duplicate slug"):
        load_city_configs(tmp_path, DEFAULTS)


def test_override_default(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID + "max_episodes: 10\n")
    cities = load_city_configs(tmp_path, DEFAULTS)
    assert cities[0].max_episodes == 10


def test_template_file_skipped(tmp_path):
    _write(tmp_path, "_template.yml", VALID)
    assert load_city_configs(tmp_path, DEFAULTS) == []


def test_alias_colliding_with_slug_raises(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID)
    bar = VALID.replace("slug: foo-tx", "slug: bar-tx").replace(
        "state: TX\n", "state: TX\naliases: [foo-tx]\n"
    )
    _write(tmp_path, "bar-tx.yml", bar)
    with pytest.raises(ValueError, match="collides with the slug"):
        load_city_configs(tmp_path, DEFAULTS)


def test_duplicate_alias_across_cities_raises(tmp_path):
    a = VALID.replace("state: TX\n", "state: TX\naliases: [old-name]\n")
    b = VALID.replace("slug: foo-tx", "slug: bar-tx").replace(
        "state: TX\n", "state: TX\naliases: [old-name]\n"
    )
    _write(tmp_path, "foo-tx.yml", a)
    _write(tmp_path, "bar-tx.yml", b)
    with pytest.raises(ValueError, match="already used"):
        load_city_configs(tmp_path, DEFAULTS)


def test_valid_alias_accepted(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID.replace("state: TX\n", "state: TX\naliases: [foo-old]\n"))
    cities = load_city_configs(tmp_path, DEFAULTS)
    assert cities[0].aliases == ["foo-old"]


# --- entity (config/cities/*.yml) merge ------------------------------------------------

ENTITY = """\
city_website: https://www.foo.gov
meetings_url: https://www.foo.gov/meetings
state: TX
colors: ["#123456", "#abcdef"]
"""

FEED_WITH_ENTITY = """\
slug: foo-tx
city: foo
provider: granicus
source:
  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2
podcast_title: Foo Council
podcast_author: City of Foo
podcast_email: ""
podcast_description: Meetings.
"""


def test_entity_fields_merged_into_feed(tmp_path):
    _write_entity(tmp_path, "foo.yml", ENTITY)
    _write(tmp_path, "foo-tx.yml", FEED_WITH_ENTITY)
    c = load_city_configs(tmp_path, DEFAULTS)[0]
    assert c.city_entity == "foo"
    assert c.city_website == "https://www.foo.gov"
    assert c.meetings_url == "https://www.foo.gov/meetings"
    assert c.state == "TX"
    assert c.colors == ["#123456", "#abcdef"]


def test_feed_level_value_overrides_entity(tmp_path):
    _write_entity(tmp_path, "foo.yml", ENTITY)
    _write(tmp_path, "foo-tx.yml", FEED_WITH_ENTITY + "meetings_url: https://override.gov\n")
    c = load_city_configs(tmp_path, DEFAULTS)[0]
    assert c.meetings_url == "https://override.gov"  # feed wins
    assert c.city_website == "https://www.foo.gov"  # entity still supplies the rest


def test_unknown_entity_reference_raises(tmp_path):
    _write(tmp_path, "foo-tx.yml", FEED_WITH_ENTITY)  # references city: foo, but none exists
    with pytest.raises(ValueError, match="unknown entity"):
        load_city_configs(tmp_path, DEFAULTS)


def test_entity_template_file_skipped(tmp_path):
    _write_entity(tmp_path, "_template.yml", ENTITY)
    _write(tmp_path, "foo-tx.yml", VALID)  # no city: ref, loads fine; entity _template ignored
    cities = load_city_configs(tmp_path, DEFAULTS)
    assert len(cities) == 1 and cities[0].city_entity is None
