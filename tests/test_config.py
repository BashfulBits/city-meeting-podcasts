"""Unit tests for config loading and validation."""

from __future__ import annotations

import textwrap
from datetime import date
from pathlib import Path

import pytest

from citypods.config import filter_city_configs, load_city_configs, load_site_config

DEFAULTS = {
    "podcast_language": "en-us",
    "podcast_category": "Government",
    "max_episodes": 500,
    "full_artifact_episodes": 2000,
    "metadata_retention_episodes": 10000,
}

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
    assert c.max_episodes == 500  # inherited default
    assert c.full_artifact_episodes == 2000
    assert c.metadata_retention_episodes == 10000
    assert c.source_id is None
    assert c.lifecycle.status == "active"


def test_loads_explicit_alternative_body_selectors(tmp_path):
    body = VALID.replace(
        "source:\n  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n",
        "source:\n"
        "  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n"
        "  body: City Council\n"
        "  body_any:\n"
        "    - Special Called City Council Meeting\n",
    )
    _write(tmp_path, "foo-tx.yml", body)
    city = load_city_configs(tmp_path, DEFAULTS)[0]
    assert city.source["body_any"] == ["Special Called City Council Meeting"]


def test_rejects_malformed_alternative_body_selectors(tmp_path):
    body = VALID.replace(
        "source:\n  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n",
        "source:\n"
        "  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n"
        "  body_any: Special Called City Council Meeting\n",
    )
    _write(tmp_path, "foo-tx.yml", body)
    with pytest.raises(ValueError, match="body_any"):
        load_city_configs(tmp_path, DEFAULTS)


def test_loads_exact_body_inclusions(tmp_path):
    body = VALID.replace(
        "source:\n  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n",
        "source:\n"
        "  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n"
        "  body: City Council\n"
        "  body_includes:\n"
        "    - provider_guid: https://foo.granicus.com/MediaPlayer.php?view_id=2&clip_id=42\n"
        "      body: Work Session\n",
    )
    _write(tmp_path, "foo-tx.yml", body)
    city = load_city_configs(tmp_path, DEFAULTS)[0]
    assert city.source["body_includes"][0]["body"] == "Work Session"


def test_rejects_malformed_body_inclusions(tmp_path):
    body = VALID.replace(
        "source:\n  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n",
        "source:\n"
        "  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n"
        "  body_includes: [Work Session]\n",
    )
    _write(tmp_path, "foo-tx.yml", body)
    with pytest.raises(ValueError, match="body_includes"):
        load_city_configs(tmp_path, DEFAULTS)


def test_source_id_is_loaded_and_path_safe(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID + "source_id: 4ea6c4b78abc\n")
    assert load_city_configs(tmp_path, DEFAULTS)[0].source_id == "4ea6c4b78abc"


def test_uid_overrides_are_loaded(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID + "uid_overrides:\n  replacement-42: 0123456789abcdef\n")
    assert load_city_configs(tmp_path, DEFAULTS)[0].uid_overrides == {
        "replacement-42": "0123456789abcdef"
    }


@pytest.mark.parametrize(
    "block",
    [
        "uid_overrides: nope\n",
        "uid_overrides:\n  replacement-42: not-a-uid\n",
        (
            "uid_overrides:\n  replacement-42: 0123456789abcdef\n"
            "  replacement-43: 0123456789abcdef\n"
        ),
    ],
)
def test_invalid_uid_overrides_raise(tmp_path, block):
    _write(tmp_path, "foo-tx.yml", VALID + block)
    with pytest.raises(ValueError, match="uid_overrides"):
        load_city_configs(tmp_path, DEFAULTS)


@pytest.mark.parametrize("source_id", ["../escape", "Upper", "has_under", "-leading", ""])
def test_invalid_source_id_raises(tmp_path, source_id):
    _write(tmp_path, "foo-tx.yml", VALID + f"source_id: {source_id!r}\n")
    with pytest.raises(ValueError, match="source_id"):
        load_city_configs(tmp_path, DEFAULTS)


def test_conflicting_source_id_reuse_raises(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID + "source_id: shared-source\n")
    other = VALID.replace("slug: foo-tx", "slug: bar-tx").replace("view_id=2", "view_id=9")
    _write(tmp_path, "bar-tx.yml", other + "source_id: shared-source\n")
    with pytest.raises(ValueError, match="source_id.*conflicts"):
        load_city_configs(tmp_path, DEFAULTS)


def test_shared_source_id_allows_feed_local_body_selectors(tmp_path):
    first = VALID.replace(
        "source:\n  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n",
        "source:\n"
        "  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n"
        "  body: City Council\n"
        "  body_any:\n"
        "    - Special Called City Council Meeting\n",
    )
    second = VALID.replace(
        "source:\n  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n",
        "source:\n"
        "  feed_url: https://foo.granicus.com/ViewPublisherRSS.php?view_id=2\n"
        "  body: City Council\n"
        "  body_includes:\n"
        "    - provider_guid: https://foo.granicus.com/MediaPlayer.php?view_id=2&clip_id=42\n"
        "      body: Work Session\n",
    ).replace("slug: foo-tx", "slug: bar-tx")
    _write(tmp_path, "foo-tx.yml", first + "source_id: shared-source\n")
    _write(tmp_path, "bar-tx.yml", second + "source_id: shared-source\n")

    assert {city.slug for city in load_city_configs(tmp_path, DEFAULTS)} == {
        "foo-tx",
        "bar-tx",
    }


@pytest.mark.parametrize(
    "block,status,checks_before,checks_after",
    [
        ("", "active", True, True),
        (
            "lifecycle:\n  status: paused\n  recheck_after: 2026-09-15\n  reason: recess\n",
            "paused",
            False,
            True,
        ),
        ("lifecycle:\n  status: dormant\n  reason: irregular body\n", "dormant", False, False),
        ("lifecycle:\n  status: retired\n  reason: dissolved\n", "retired", False, False),
    ],
)
def test_lifecycle_policy(tmp_path, block, status, checks_before, checks_after):
    _write(tmp_path, "foo-tx.yml", VALID + block)
    lifecycle = load_city_configs(tmp_path, DEFAULTS)[0].lifecycle
    assert lifecycle.status == status
    assert lifecycle.polls_provider() is (status != "retired")
    assert lifecycle.checks_staleness(date(2026, 9, 14)) is checks_before
    assert lifecycle.checks_staleness(date(2026, 9, 15)) is checks_after


@pytest.mark.parametrize(
    "block,match",
    [
        ("lifecycle:\n  status: paused\n  reason: recess\n", "requires recheck_after"),
        (
            "lifecycle:\n  status: dormant\n  recheck_after: 2026-09-15\n  reason: x\n",
            "allowed only for paused",
        ),
        ("lifecycle:\n  status: retired\n", "requires a reason"),
        ("lifecycle:\n  status: unknown\n", "lifecycle.status"),
        (
            "lifecycle:\n  status: dormant\n  reason: x\n  evidence_url: http://bad.test\n",
            "https only",
        ),
    ],
)
def test_invalid_lifecycle_raises(tmp_path, block, match):
    _write(tmp_path, "foo-tx.yml", VALID + block)
    with pytest.raises((ValueError, Exception), match=match):
        load_city_configs(tmp_path, DEFAULTS)


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


def test_auxiliary_provider_and_source_are_loaded_and_validated(tmp_path):
    body = VALID.replace(
        "podcast_description: Meetings.\n",
        "podcast_description: Meetings.\n"
        "aux_provider: legistar\n"
        "aux_source:\n"
        "  calendar_url: https://foo.legistar.com/Calendar.aspx\n"
        "  granicus_base: https://foo.granicus.com\n"
        "  backfill_since: '2024-01-01'\n"
        "  view_id: 1\n",
    )
    _write(tmp_path, "foo-tx.yml", body)

    city = load_city_configs(tmp_path, DEFAULTS)[0]
    assert city.aux_provider == "legistar"
    assert city.aux_source == {
        "calendar_url": "https://foo.legistar.com/Calendar.aspx",
        "granicus_base": "https://foo.granicus.com",
        "backfill_since": "2024-01-01",
        "view_id": 1,
    }


@pytest.mark.parametrize(
    "addition, match",
    [
        ("aux_provider: legistar\n", "set together"),
        ("aux_source: {}\n", "set together"),
    ],
)
def test_auxiliary_source_fields_must_be_set_together(tmp_path, addition, match):
    _write(tmp_path, "foo-tx.yml", VALID + addition)
    with pytest.raises(ValueError, match=match):
        load_city_configs(tmp_path, DEFAULTS)


def test_auxiliary_source_inherits_from_city_entity(tmp_path):
    _write_entity(
        tmp_path,
        "foo-tx.yml",
        """
        aux_provider: legistar
        aux_source:
          calendar_url: https://foo.legistar.com/Calendar.aspx
          granicus_base: https://foo.granicus.com
          view_id: 1
          backfill_since: '2010-01-01'
        """,
    )
    _write(tmp_path, "foo-tx.yml", VALID.replace("state: TX\n", "city: foo-tx\n"))

    city = load_city_configs(tmp_path, DEFAULTS)[0]
    assert city.aux_provider == "legistar"
    assert city.aux_source["calendar_url"] == "https://foo.legistar.com/Calendar.aspx"


@pytest.mark.parametrize(
    "bad_slug",
    ["../etc/passwd", "foo/bar", "foo.tx", "Foo-TX", "foo_tx", "/etc/passwd", ""],
)
def test_slug_with_path_traversal_or_bad_format_raises(tmp_path, bad_slug):
    # CR2-CP-49: slug feeds directly into output_dir / city.slug (run.py) — a slug containing
    # "..", "/", or other non-slug characters from a mistyped/compromised config file must be
    # rejected at load time, not accepted and later used for path construction.
    _write(tmp_path, "foo-tx.yml", VALID.replace("slug: foo-tx", f"slug: {bad_slug!r}"))
    with pytest.raises(ValueError, match="slug"):
        load_city_configs(tmp_path, DEFAULTS)


def test_alias_with_path_traversal_raises(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID + "aliases: ['../escape']\n")
    with pytest.raises(ValueError, match="alias"):
        load_city_configs(tmp_path, DEFAULTS)


def test_valid_slug_and_alias_format_accepted(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID + "aliases: ['old-foo-tx', 'foo-tx-2']\n")
    cities = load_city_configs(tmp_path, DEFAULTS)
    assert cities[0].aliases == ["old-foo-tx", "foo-tx-2"]


def test_asr_workers_zero_rejected(tmp_path):
    # M1/CR2-CP-43: stages.py divides cpu_count() / city.asr_workers; asr_workers: 0 must fail
    # at config load, not as a ZeroDivisionError at runtime mid-shard.
    _write(tmp_path, "foo-tx.yml", VALID + "asr_workers: 0\n")
    with pytest.raises(ValueError, match="asr_workers"):
        load_city_configs(tmp_path, DEFAULTS)


def test_duplicate_slug_raises(tmp_path):
    _write(tmp_path, "a.yml", VALID)
    _write(tmp_path, "b.yml", VALID)
    with pytest.raises(ValueError, match="duplicate slug"):
        load_city_configs(tmp_path, DEFAULTS)


@pytest.mark.parametrize(
    "key",
    ["max_episodes", "full_artifact_episodes", "metadata_retention_episodes", "max_archive_items"],
)
def test_feed_retention_override_is_rejected(tmp_path, key):
    _write(tmp_path, "foo-tx.yml", VALID + f"{key}: 10\n")
    with pytest.raises(ValueError, match="retention is configured only"):
        load_city_configs(tmp_path, DEFAULTS)


def test_retention_defaults_reject_invalid_ordering(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID)
    bad_defaults = {**DEFAULTS, "max_episodes": 3000}  # > full_artifact_episodes (2000)
    with pytest.raises(ValueError, match="require 0 < max_episodes"):
        load_city_configs(tmp_path, bad_defaults)


def test_asr_alignment_defaults_off_and_can_be_enabled(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID)
    assert load_city_configs(tmp_path, DEFAULTS)[0].asr_alignment_enabled is False

    _write(
        tmp_path,
        "foo-tx.yml",
        VALID + "asr_alignment_enabled: true\n",
    )
    assert load_city_configs(tmp_path, DEFAULTS)[0].asr_alignment_enabled is True


def test_asr_alignment_interpolation_uses_feed_over_default(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID + "asr_alignment_interpolate: nearest\n")
    defaults = {**DEFAULTS, "asr_alignment_interpolate": "ignore"}
    assert load_city_configs(tmp_path, defaults)[0].asr_alignment_interpolate == "nearest"


@pytest.mark.parametrize("value", ["typo", None, 1])
def test_asr_alignment_interpolation_rejects_invalid_feed_value(tmp_path, value):
    rendered = "null" if value is None else str(value)
    _write(tmp_path, "foo-tx.yml", VALID + f"asr_alignment_interpolate: {rendered}\n")
    with pytest.raises(ValueError, match="asr_alignment_interpolate"):
        load_city_configs(tmp_path, DEFAULTS)


def test_asr_alignment_interpolation_rejects_unhashable_feed_value(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID + "asr_alignment_interpolate: [nearest]\n")
    with pytest.raises(ValueError, match="asr_alignment_interpolate"):
        load_city_configs(tmp_path, DEFAULTS)


def test_asr_alignment_interpolation_rejects_invalid_default(tmp_path):
    _write(tmp_path, "foo-tx.yml", VALID)
    with pytest.raises(ValueError, match="asr_alignment_interpolate"):
        load_city_configs(tmp_path, {**DEFAULTS, "asr_alignment_interpolate": "typo"})


def test_production_local_asr_duration_limit_is_four_hours():
    config = load_site_config(Path(__file__).resolve().parents[1] / "config" / "site_config.yml")
    assert config["defaults"]["asr_local_max_duration_hours"] == 4


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


def test_filter_city_configs_accepts_feed_or_entity_slug(tmp_path):
    _write_entity(tmp_path, "foo.yml", ENTITY)
    _write(
        tmp_path,
        "foo-council.yml",
        FEED_WITH_ENTITY.replace("slug: foo-tx", "slug: foo-council"),
    )
    _write(
        tmp_path,
        "foo-planning.yml",
        FEED_WITH_ENTITY.replace("slug: foo-tx", "slug: foo-planning"),
    )
    cities = load_city_configs(tmp_path, DEFAULTS)

    assert [c.slug for c in filter_city_configs(cities, "foo-council")] == ["foo-council"]
    assert [c.slug for c in filter_city_configs(cities, "foo")] == ["foo-council", "foo-planning"]
    assert filter_city_configs(cities, "missing") == []


def test_unknown_audit_block_is_preserved_in_city_extra(tmp_path):
    _write(
        tmp_path,
        "foo-tx.yml",
        VALID
        + "audit:\n"
        + "  lifecycle:\n"
        + "    status: inactive\n"
        + "    verified_at: 2026-07-11\n",
    )

    c = load_city_configs(tmp_path, DEFAULTS)[0]

    assert c.extra["audit"]["lifecycle"]["status"] == "inactive"
