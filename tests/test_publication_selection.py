"""Frozen proof and failure behavior for inactive publication selection."""

import copy
import hashlib
import json
from dataclasses import replace

import pytest

from citypods.models import City
from citypods.publication_selection import (
    load_selection_index,
    parse_publication_selection,
    record_identity_fingerprint,
    select_feed_publication,
    select_search_publication,
)
from citypods.records import source_key


def packet():
    city = City(
        slug="test",
        provider="granicus",
        source={},
        podcast_title="Test",
        podcast_author="Test",
        podcast_email="",
        podcast_description="Test",
    )
    city = replace(city, source={**city.source, "body": "Council"}, extra={})
    key = source_key(city)
    records = {}
    for uid in ("a" * 16, "b" * 16):
        records[uid] = dict(
            uid=uid,
            provider_guid="official-guid",
            body="Council",
            title="Meeting",
            published="2026-01-01T12:00:00Z",
            video_url="https://example.org/video",
        )
    ref = dict(
        url="https://example.org/agenda", retrieved_at="2026-01-02T00:00:00Z", content_hash="c" * 64
    )
    group = dict(
        id="one",
        source_key=key,
        identity_kind="same_provider_guid",
        identity_key="official-guid",
        members=[
            dict(
                uid=uid,
                provider_guid=r["provider_guid"],
                record_fingerprint=record_identity_fingerprint(key, r),
            )
            for uid, r in records.items()
        ],
        preferred_uid="a" * 16,
        evidence_refs=[ref],
        approval_ref="https://github.com/o/r/issues/1",
        exposure=dict(status="historical-unknown", artifacts=[], rationale="Unknown past"),
        search=True,
        date_resolution=None,
    )
    city.extra["publication_selection"] = dict(version=1, groups=[group])
    return city, records, group


def test_fingerprint_exact_vector():
    r = dict(
        uid="a",
        provider_guid="https://example.org/player",
        title="Exact",
        body=None,
        published="2026-01-01T00:00:00Z",
        video_url="https://ignored.org",
    )
    expected = dict(
        source_key="source",
        uid="a",
        provider_guid=r["provider_guid"],
        title="Exact",
        body=None,
        published=r["published"],
        recording_url=r["provider_guid"],
    )
    assert (
        record_identity_fingerprint("source", r)
        == hashlib.sha256(
            json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


def test_projection_preserves_objects_and_orders():
    city, records, _ = packet()
    index = load_selection_index([city])
    plan = select_search_publication(index, source_key(city), records)
    assert not plan.held
    assert plan.public_items == (records["a" * 16],)
    assert plan.public_items[0] is records["a" * 16]
    assert plan.owner_by_uid["a" * 16] is city
    feed = select_feed_publication(index, city, tuple(records.values()), records)
    assert feed.public_items == plan.public_items
    assert len(records) == 2


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing", "missing-preferred"),
        ("fingerprint", "fingerprint-changed"),
        ("guid", "guid-mismatch"),
        ("extra", "unexpected-member"),
        ("body", "owner-selector-mismatch"),
        ("date", "unresolved-date-conflict"),
    ],
)
def test_proof_failures_hold(change, code):
    city, records, _ = packet()
    index = load_selection_index([city])
    if change == "missing":
        records.pop("a" * 16)
    elif change == "extra":
        records["d" * 16] = {**records["a" * 16], "uid": "d" * 16, "body": "Other"}
    else:
        field, value = {
            "fingerprint": ("title", "Changed"),
            "guid": ("provider_guid", "other"),
            "body": ("body", "Other"),
            "date": ("published", "2026-02-01"),
        }[change]
        records["a" * 16][field] = value
    plan = select_search_publication(index, source_key(city), records)
    assert plan.held
    assert code in {d.code for d in plan.diagnostics}
    assert plan.public_items == plan.raw_items


@pytest.mark.parametrize(
    "raw", [None, {"version": True, "groups": []}, {"version": 1, "groups": [], "extra": 1}]
)
def test_strict_top_level(raw):
    with pytest.raises(ValueError):
        parse_publication_selection(raw, source_key="a" * 12, feed_slug="feed")


def test_reordering_is_not_precedence():
    city, records, group = packet()
    first = select_search_publication(load_selection_index([city]), source_key(city), records)
    group["members"].reverse()
    second = select_search_publication(load_selection_index([city]), source_key(city), records)
    assert first.policy_hash == second.policy_hash


def test_conflicting_owner_and_extra_nested_keys():
    city, _, group = packet()
    other = replace(city, slug="another")
    with pytest.raises(ValueError, match="conflicting"):
        load_selection_index([city, other])
    group["members"][0]["extra"] = 1
    with pytest.raises(ValueError):
        load_selection_index([city])


def test_no_config_preserves_records():
    city, records, _ = packet()
    city.extra.clear()
    plan = select_search_publication(load_selection_index([city]), source_key(city), records)
    assert plan.policy_hash is None
    assert plan.public_items == tuple(records.values())


def test_granicus_verified_views_and_wrong_identity():
    city, records, group = packet()
    group["identity_kind"] = "verified_granicus_clip"
    group["identity_key"] = "https://example.org/clip/42"
    for view, (uid, record) in enumerate(records.items(), 1):
        record["provider_guid"] = f"https://example.org/MediaPlayer.php?view_id={view}&clip_id=42"
        for member in group["members"]:
            if member["uid"] == uid:
                member["provider_guid"] = record["provider_guid"]
                member["record_fingerprint"] = record_identity_fingerprint(source_key(city), record)
    index = load_selection_index([city])
    assert not select_search_publication(index, source_key(city), records).held
    changed = copy.deepcopy(records)
    changed["b" * 16]["provider_guid"] += "&clip_id=42"
    plan = select_search_publication(index, source_key(city), changed)
    assert "identity-mismatch" in {d.code for d in plan.diagnostics}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda g: g.update(source_key="f" * 12),
        lambda g: g.update(search=1),
        lambda g: g.update(preferred_uid=[]),
        lambda g: g["exposure"].update(status="known-current"),
        lambda g: g["evidence_refs"][0].update(retrieved_at="2026-01-01T00:00:00"),
        lambda g: g.update(
            date_resolution={
                "official_date": "2026-01-01",
                "evidence_url": "https://example.org/not-reviewed",
            }
        ),
    ],
)
def test_nested_schema_validation(mutation):
    city, _, group = packet()
    mutation(group)
    with pytest.raises(ValueError):
        load_selection_index([city])


def test_date_resolution_validates_preferred_without_rewriting():
    city, records, group = packet()
    records["b" * 16]["published"] = "2026-01-02T12:00:00Z"
    group["members"][1]["record_fingerprint"] = record_identity_fingerprint(
        source_key(city), records["b" * 16]
    )
    group["date_resolution"] = {
        "official_date": "2026-01-01",
        "evidence_url": "https://example.org/agenda",
    }
    before = copy.deepcopy(records)
    assert not select_search_publication(
        load_selection_index([city]), source_key(city), records
    ).held
    assert records == before
    group["date_resolution"]["official_date"] = "2026-01-02"
    assert select_search_publication(load_selection_index([city]), source_key(city), records).held


def test_same_numeric_clip_other_host_is_independent():
    city, records, group = packet()
    group["identity_kind"] = "verified_granicus_clip"
    group["identity_key"] = "https://example.org/clip/42"
    for view, (uid, record) in enumerate(records.items(), 1):
        record["provider_guid"] = f"https://example.org/MediaPlayer.php?view_id={view}&clip_id=42"
        member = next(m for m in group["members"] if m["uid"] == uid)
        member["provider_guid"] = record["provider_guid"]
        member["record_fingerprint"] = record_identity_fingerprint(source_key(city), record)
    records["d" * 16] = {
        **records["a" * 16],
        "uid": "d" * 16,
        "provider_guid": "https://other.org/MediaPlayer.php?view_id=1&clip_id=42",
    }
    plan = select_search_publication(load_selection_index([city]), source_key(city), records)
    assert not plan.held
    assert plan.selected_uids == ("a" * 16, "d" * 16)


def test_shared_nonsearch_group_and_immutable_plan():
    city, records, group = packet()
    group["search"] = False
    other = replace(city, slug="another")
    index = load_selection_index([city, other])
    assert len(index.groups) == 2
    plan = select_feed_publication(index, city, records.values(), records)
    assert not plan.held
    with pytest.raises(TypeError):
        plan.owner_by_uid["new"] = city
    with pytest.raises(TypeError):
        index.owners["new"] = city
    assert select_search_publication(index, source_key(city), records).policy_hash is None


def test_stored_uid_must_match_member_key_even_with_matching_fingerprint():
    city, records, group = packet()
    records["a" * 16]["uid"] = "c" * 16
    group["members"][0]["record_fingerprint"] = record_identity_fingerprint(
        source_key(city), records["a" * 16]
    )
    plan = select_search_publication(load_selection_index([city]), source_key(city), records)
    assert plan.held
    assert "identity-mismatch" in {d.code for d in plan.diagnostics}


def test_equal_dates_require_null_date_resolution():
    city, records, group = packet()
    group["date_resolution"] = {
        "official_date": "2026-01-01",
        "evidence_url": "https://example.org/agenda",
    }
    plan = select_search_publication(load_selection_index([city]), source_key(city), records)
    assert plan.held
    assert "unresolved-date-conflict" in {d.code for d in plan.diagnostics}
