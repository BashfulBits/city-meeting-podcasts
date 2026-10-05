"""Archive dispositions are exact, source scoped and reversible projection inputs."""

import copy
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from citypods.archive_visibility import (
    archive_policy_hash,
    is_archive_only,
    load_archive_index,
    parse_archive_only,
)
from citypods.models import City, Episode
from citypods.records import source_key


def declaration():
    return dict(
        uid="a" * 16, provider_guid="123", reason="Film", approval_ref="Maintainer approval"
    )


def city(slug="one", source_id="source-one", entries=None):
    return City(
        slug=slug,
        source_id=source_id,
        provider="swagit",
        source={"url": "https://example.org"},
        podcast_title="Test",
        podcast_author="Test",
        podcast_email="",
        podcast_description="Test",
        extra={"archive_only": [declaration()] if entries is None else entries},
    )


@pytest.mark.parametrize("raw", [None, {}, "a", [None], [{}], [dict(declaration(), extra="x")]])
def test_rejects_malformed_declarations(raw):
    with pytest.raises(ValueError, match="archive_only"):
        parse_archive_only(raw)


@pytest.mark.parametrize("field", ["uid", "provider_guid", "reason", "approval_ref"])
@pytest.mark.parametrize("value", ["", "  ", None, 123, True])
def test_all_fields_require_nonempty_strings(field, value):
    raw = declaration()
    raw[field] = value
    with pytest.raises(ValueError, match=field):
        parse_archive_only([raw])


def test_exact_duplicates_are_allowed_but_conflicting_declarations_fail():
    first = declaration()
    assert len(parse_archive_only([first, first.copy()])) == 1
    assert len(load_archive_index([city(), city("two")])["source-one"]) == 1
    for field in ("provider_guid", "reason", "approval_ref"):
        other = dict(first, **{field: "different"})
        with pytest.raises(ValueError, match="conflicting"):
            parse_archive_only([first, other])
        with pytest.raises(ValueError, match="conflicting"):
            load_archive_index([city(), city("two", entries=[other])])


def test_source_scope_exact_identity_and_unchanged_records():
    configured = city()
    record = dict(declaration(), title="Film", audio_key="immutable/audio.m4a")
    before = copy.deepcopy(record)
    index = load_archive_index([configured])
    assert is_archive_only(index, source_key(configured), record)
    assert not is_archive_only(index, "other-source", record)
    assert not is_archive_only(index, source_key(configured), dict(record, uid="b" * 16))
    with pytest.raises(ValueError, match="GUID mismatch"):
        is_archive_only(index, source_key(configured), dict(record, provider_guid="456"))
    assert record == before
    episode = Episode(
        guid="123",
        uid="a" * 16,
        title="Film",
        video_url="https://example.org/video",
        published=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert is_archive_only(index, source_key(configured), episode)
    with pytest.raises(ValueError, match="GUID mismatch"):
        is_archive_only(index, source_key(configured), replace(episode, guid="456"))


def test_hash_changes_for_policy_removal_restoration_and_approval_edits():
    configured = city()
    index = load_archive_index([configured])
    original = archive_policy_hash(index, "source-one")
    assert original and len(original) == 64
    assert archive_policy_hash(load_archive_index([city(entries=[])]), "source-one") is None
    assert archive_policy_hash(load_archive_index([configured]), "source-one") == original
    assert archive_policy_hash(index, "other-source") is None
    altered = city(entries=[dict(declaration(), approval_ref="Revised approval")])
    assert archive_policy_hash(load_archive_index([altered]), "source-one") != original


def test_conflicts_are_independent_across_sources_and_index_is_immutable():
    other = city("two", "source-two", [dict(declaration(), reason="Other disposition")])
    index = load_archive_index([city(), other])
    assert len(index) == 2
    with pytest.raises(TypeError):
        index["source-one"]["a" * 16]["reason"] = "Changed"


def test_hash_does_not_depend_on_feed_or_declaration_order():
    first = city(entries=[declaration(), dict(declaration(), uid="b" * 16)])
    second = city("two", entries=list(reversed(first.extra["archive_only"])))
    assert archive_policy_hash(load_archive_index([first, second]), "source-one") == (
        archive_policy_hash(load_archive_index([second, first]), "source-one")
    )


def test_config_loader_rejects_malformed_and_cross_feed_conflicts(tmp_path):
    import yaml

    from citypods.config import load_city_configs

    feeds = tmp_path / "feeds"
    feeds.mkdir()
    raw = dict(
        slug="test-one",
        provider="granicus",
        source={"feed_url": "https://foo.granicus.com/ViewPublisherRSS.php?view_id=2"},
        podcast_title="Test",
        podcast_author="Town",
        podcast_email="",
        podcast_description="Meetings",
        archive_only=[declaration()],
    )
    first = feeds / "one.yml"
    first.write_text(yaml.safe_dump(dict(raw, archive_only=None)))
    with pytest.raises(ValueError, match="one.yml: archive_only"):
        load_city_configs(tmp_path, {})
    first.write_text(yaml.safe_dump(raw))
    second = feeds / "two.yml"
    second.write_text(yaml.safe_dump(dict(raw, slug="test-two")))
    assert len(load_city_configs(tmp_path, {})) == 2
    second.write_text(
        yaml.safe_dump(
            dict(
                raw,
                slug="test-two",
                archive_only=[dict(declaration(), reason="Conflict")],
            )
        )
    )
    with pytest.raises(ValueError, match="conflicting declaration"):
        load_city_configs(tmp_path, {})


def test_approved_addison_config_archives_only_six_original_identities():
    """The actual feed configuration binds all six approvals without rewriting raw metadata."""
    import json
    from pathlib import Path

    from citypods.config import load_city_configs

    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    index = load_archive_index(cities)
    records = json.loads((root / "tests/fixtures/addison-archive-only.json").read_text())
    original = copy.deepcopy(records)
    assert set(index["abbf5e25e078"]) == set(records)
    assert len(records) == 6
    for record in records.values():
        assert is_archive_only(index, "abbf5e25e078", record)
        assert not is_archive_only(index, "another-source", record)
    assert records == original


def test_luncheon_raw_archive_route_keeps_public_exclusion():
    """Exact raw routing creates a page without granting public discovery."""
    import json
    from pathlib import Path

    from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions
    from citypods.config import load_city_configs

    root = Path(__file__).resolve().parents[1]
    cities = load_city_configs(root / "config", {})
    city = next(c for c in cities if c.slug == "addison-tx-town-meetings")
    records = json.loads((root / "tests/fixtures/addison-archive-only.json").read_text())
    record = records["35a78fa89c7c5c5c"]
    assert record_matches_body(
        record, source_body_filter(city.source), source_body_inclusions(city.source)
    )
    assert is_archive_only(load_archive_index(cities), source_key(city), record)
    assert record["audio"]["key"] == "swagit/abbf5e25e078/35a78fa89c7c5c5c-b60288bf7dfc.m4a"
    assert record["audio"]["url"] == (
        "https://audio.citymeetings.fyi/swagit/abbf5e25e078/"
        "35a78fa89c7c5c5c-b60288bf7dfc.m4a"
    )
    wrong = dict(record, provider_guid="not-the-luncheon")
    assert not record_matches_body(
        wrong, source_body_filter(city.source), source_body_inclusions(city.source)
    )
