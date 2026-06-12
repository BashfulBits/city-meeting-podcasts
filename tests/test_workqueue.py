"""Tests for the H5 backlog ordering engine (citypods/ops/workqueue.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from citypods.models import Episode
from citypods.ops.workqueue import (
    BUCKET_DEEP_ARCHIVE,
    BUCKET_FEED_VISIBLE,
    BacklogPolicy,
    WorkItem,
    order,
    workitem_from_episode,
)

NOW = datetime(2026, 6, 11, tzinfo=UTC)


def _wi(uid, *, days_ago=0, city="", body="", bucket=BUCKET_FEED_VISIBLE):
    return WorkItem(
        source_key=city or "s",
        episode_uid=uid,
        work_class="audio",
        published=NOW - timedelta(days=days_ago),
        city_slug=city,
        body=body,
        priority_bucket=bucket,
    )


def _policy(*entries, city_order=None):
    cfg = {"backlog_priority": list(entries)}
    if city_order is not None:
        cfg["city_order"] = city_order
    return BacklogPolicy.from_site_config(cfg, now=NOW)


def _uids(items):
    return [it.episode_uid for it in items]


# --------------------------------------------------------------------------------------------
# order() — identity / stability
# --------------------------------------------------------------------------------------------


def test_order_identity_when_no_policy():
    items = [_wi("a", days_ago=5), _wi("b", days_ago=1), _wi("c", days_ago=9)]
    assert order(items, None) == items
    assert order(items, BacklogPolicy()) == items  # empty policy == identity


def test_order_returns_new_list_not_alias():
    items = [_wi("a")]
    assert order(items, None) is not items


def test_order_is_stable_on_ties():
    # All same day ⇒ recency can't differentiate ⇒ input order preserved.
    items = [_wi("a", days_ago=2), _wi("b", days_ago=2), _wi("c", days_ago=2)]
    assert _uids(order(items, _policy({"recency": "desc"}))) == ["a", "b", "c"]


# --------------------------------------------------------------------------------------------
# recency
# --------------------------------------------------------------------------------------------


def test_recency_desc_newest_first():
    items = [_wi("old", days_ago=10), _wi("new", days_ago=1), _wi("mid", days_ago=5)]
    assert _uids(order(items, _policy({"recency": "desc"}))) == ["new", "mid", "old"]


def test_recency_asc_oldest_first():
    items = [_wi("old", days_ago=10), _wi("new", days_ago=1), _wi("mid", days_ago=5)]
    assert _uids(order(items, _policy({"recency": "asc"}))) == ["old", "mid", "new"]


def test_recency_bare_string_defaults_desc():
    items = [_wi("old", days_ago=10), _wi("new", days_ago=1)]
    assert _uids(order(items, _policy("recency"))) == ["new", "old"]


def test_recency_missing_date_sorts_last():
    none_item = WorkItem("s", "nodate", "audio", published=None)
    items = [none_item, _wi("new", days_ago=1), _wi("old", days_ago=20)]
    assert _uids(order(items, _policy({"recency": "desc"}))) == ["new", "old", "nodate"]


def test_recency_within_days_collapses_old_to_tiebreak():
    # Within 30d: newest-first. Beyond 30d: collapse to a tie so the NEXT key (city_order) governs.
    items = [
        _wi("recent_b", days_ago=3, city="dallas-tx"),
        _wi("recent_a", days_ago=1, city="dallas-tx"),
        _wi("old_dallas", days_ago=100, city="dallas-tx"),
        _wi("old_denton", days_ago=200, city="denton-tx"),
    ]
    policy = _policy(
        {"recency": {"order": "desc", "within_days": 30}},
        "city_order",
        city_order=["denton-tx", "dallas-tx"],
    )
    # recent ones newest-first; then the OLD ones ordered by city_order (denton before dallas)
    assert _uids(order(items, policy)) == ["recent_a", "recent_b", "old_denton", "old_dallas"]


def test_recency_within_days_zero_rejected():
    with pytest.raises(ValueError):
        _policy({"recency": {"order": "desc", "within_days": 0}})


# --------------------------------------------------------------------------------------------
# recent_first (boolean bucket)
# --------------------------------------------------------------------------------------------


def test_recent_first_buckets_then_next_key():
    items = [
        _wi("old1", days_ago=40, city="b"),
        _wi("recent_old", days_ago=20, city="z"),
        _wi("recent_new", days_ago=2, city="a"),
    ]
    # recent bucket first; WITHIN the bucket the next key (city_order) governs (not date).
    policy = _policy({"recent_first": 30}, "city_order", city_order=["a", "z", "b"])
    assert _uids(order(items, policy)) == ["recent_new", "recent_old", "old1"]


def test_recent_first_requires_count():
    with pytest.raises(ValueError):
        _policy("recent_first")


# --------------------------------------------------------------------------------------------
# city_order
# --------------------------------------------------------------------------------------------


def test_city_order_full_list():
    items = [_wi("d", city="dallas-tx"), _wi("e", city="denton-tx"), _wi("f", city="fort-worth-tx")]
    policy = _policy("city_order", city_order=["denton-tx", "dallas-tx", "fort-worth-tx"])
    assert _uids(order(items, policy)) == ["e", "d", "f"]


def test_city_order_partial_list_falls_through_to_next_key():
    # denton + dallas named; fort-worth + arlington unnamed → share a sentinel rank → ordered by
    # the next key (recency desc) among themselves, AFTER the named cities.
    items = [
        _wi("arlington_new", days_ago=1, city="arlington-tx"),
        _wi("fortworth_old", days_ago=9, city="fort-worth-tx"),
        _wi("dallas", days_ago=5, city="dallas-tx"),
        _wi("denton", days_ago=5, city="denton-tx"),
    ]
    policy = _policy("city_order", {"recency": "desc"}, city_order=["denton-tx", "dallas-tx"])
    assert _uids(order(items, policy)) == ["denton", "dallas", "arlington_new", "fortworth_old"]


def test_city_order_inline_list_overrides_top_level():
    items = [_wi("a", city="a-tx"), _wi("b", city="b-tx")]
    policy = _policy({"city_order": ["b-tx", "a-tx"]})
    assert _uids(order(items, policy)) == ["b", "a"]


# --------------------------------------------------------------------------------------------
# body_order / feed_visible_first
# --------------------------------------------------------------------------------------------


def test_body_order_alphabetical_default():
    items = [_wi("z", body="Zoning Commission"), _wi("c", body="City Council")]
    assert _uids(order(items, _policy("body_order"))) == ["c", "z"]


def test_body_order_inline_list():
    items = [_wi("z", body="Zoning Commission"), _wi("c", body="City Council")]
    policy = _policy({"body_order": ["Zoning Commission", "City Council"]})
    assert _uids(order(items, policy)) == ["z", "c"]


def test_feed_visible_first():
    items = [
        _wi("archive", bucket=BUCKET_DEEP_ARCHIVE),
        _wi("visible", bucket=BUCKET_FEED_VISIBLE),
    ]
    assert _uids(order(items, _policy("feed_visible_first"))) == ["visible", "archive"]


# --------------------------------------------------------------------------------------------
# Worked examples from review/12 §H5
# --------------------------------------------------------------------------------------------


def test_worked_example_recency_first_same_day_tie_breaks_by_city():
    # recency:desc + city_order ⇒ newest meeting from EITHER city first; a same-day tie ⇒
    # Denton before Dallas.
    items = [
        _wi("dallas_same", days_ago=2, city="dallas-tx"),
        _wi("denton_same", days_ago=2, city="denton-tx"),
        _wi("newest", days_ago=0, city="dallas-tx"),
    ]
    policy = _policy({"recency": "desc"}, "city_order", city_order=["denton-tx", "dallas-tx"])
    assert _uids(order(items, policy)) == ["newest", "denton_same", "dallas_same"]


def test_worked_example_city_greedy_order():
    # city_order FIRST ⇒ all Denton (newest-first), then all Dallas, then the rest by recency.
    items = [
        _wi("dallas_new", days_ago=1, city="dallas-tx"),
        _wi("denton_old", days_ago=30, city="denton-tx"),
        _wi("denton_new", days_ago=2, city="denton-tx"),
        _wi("other", days_ago=0, city="arlington-tx"),
    ]
    policy = _policy("city_order", {"recency": "desc"}, city_order=["denton-tx", "dallas-tx"])
    assert _uids(order(items, policy)) == ["denton_new", "denton_old", "dallas_new", "other"]


# --------------------------------------------------------------------------------------------
# from_site_config parsing
# --------------------------------------------------------------------------------------------


def test_from_site_config_production_policy_parses():
    cfg = {"backlog_priority": [{"recency": {"order": "desc", "within_days": 30}}]}
    policy = BacklogPolicy.from_site_config(cfg, now=NOW)
    assert len(policy.keys) == 1
    assert policy.keys[0].name == "recency"


def test_from_site_config_empty_is_identity():
    assert BacklogPolicy.from_site_config({}).keys == ()
    assert BacklogPolicy.from_site_config({"backlog_priority": []}).keys == ()


def test_from_site_config_reads_from_defaults_block():
    cfg = {"defaults": {"backlog_priority": ["recency"], "city_order": ["a"]}}
    assert len(BacklogPolicy.from_site_config(cfg, now=NOW).keys) == 1


def test_from_site_config_unknown_key_raises():
    with pytest.raises(ValueError, match="unknown backlog_priority comparator"):
        BacklogPolicy.from_site_config({"backlog_priority": ["nonsense"]}, now=NOW)


def test_from_site_config_reserved_key_raises():
    with pytest.raises(ValueError, match="reserved but not yet implemented"):
        BacklogPolicy.from_site_config({"backlog_priority": ["population"]}, now=NOW)


def test_from_site_config_bad_entry_raises():
    with pytest.raises(ValueError, match="invalid backlog_priority entry"):
        BacklogPolicy.from_site_config({"backlog_priority": [{"a": 1, "b": 2}]}, now=NOW)


# --------------------------------------------------------------------------------------------
# workitem_from_episode adapter
# --------------------------------------------------------------------------------------------


def _ep(guid, days_ago, body=None, uid=None):
    return Episode(
        guid=guid,
        title="Meeting",
        published=NOW - timedelta(days=days_ago),
        video_url="https://x/v.mp4",
        body=body,
        uid=uid,
    )


def test_workitem_from_episode_prefers_uid_then_guid():
    assert workitem_from_episode(_ep("g1", 1, uid="u1")).episode_uid == "u1"
    assert workitem_from_episode(_ep("g1", 1)).episode_uid == "g1"


def test_workitem_from_episode_carries_fields():
    wi = workitem_from_episode(_ep("g1", 3, body="City Council"), city_slug="denton-tx")
    assert wi.city_slug == "denton-tx"
    assert wi.body == "City Council"
    assert wi.priority_bucket == BUCKET_FEED_VISIBLE


# --------------------------------------------------------------------------------------------
# Integration with _materialize_set
# --------------------------------------------------------------------------------------------


def test_materialize_set_default_is_legacy_order():
    from citypods.stages import _materialize_set

    # Two bodies; bodyA appears first in input. Legacy order = body-grouped, newest-first per body.
    eps = [
        _ep("a_old", 10, body="City Council"),
        _ep("b_one", 5, body="Zoning Commission"),
        _ep("a_new", 1, body="City Council"),
    ]
    out = _materialize_set(eps, 50)  # no policy
    assert [e.guid for e in out] == ["a_new", "a_old", "b_one"]


def test_materialize_set_windowed_recency_reorders_across_bodies():
    from citypods.stages import _materialize_set

    eps = [
        _ep("a_old", 10, body="City Council"),
        _ep("b_one", 5, body="Zoning Commission"),
        _ep("a_new", 1, body="City Council"),
    ]
    policy = BacklogPolicy.from_site_config(
        {"backlog_priority": [{"recency": {"order": "desc", "within_days": 30}}]}, now=NOW
    )
    out = _materialize_set(eps, 50, policy=policy, city_slug="denton-tx")
    # Now globally newest-first across both bodies (all within the 30d window).
    assert [e.guid for e in out] == ["a_new", "b_one", "a_old"]


def test_materialize_set_selection_unchanged_by_policy():
    from citypods.stages import _materialize_set

    # max_per_body cap still applies identically regardless of policy (selection, not just order).
    eps = [_ep(f"c{i}", i, body="City Council") for i in range(5)]
    policy = BacklogPolicy.from_site_config({"backlog_priority": ["recency"]}, now=NOW)
    base = {e.guid for e in _materialize_set(eps, 2)}
    with_policy = {e.guid for e in _materialize_set(eps, 2, policy=policy)}
    assert base == with_policy
    assert len(base) == 2
