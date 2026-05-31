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
    (dir_ / name).write_text(textwrap.dedent(body))


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
