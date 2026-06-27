"""Tests for the H5 backlog ordering engine (citypods/ops/workqueue.py)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from citypods.models import City, Episode
from citypods.ops.workqueue import (
    BUCKET_DEEP_ARCHIVE,
    BUCKET_FEED_VISIBLE,
    BacklogPolicy,
    WorkItem,
    build_manifest,
    is_leased,
    lease,
    load_manifest,
    manifest_counts,
    order,
    order_cities_by_policy,
    release,
    save_manifest,
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


# --------------------------------------------------------------------------------------------
# Manifest derivation (build_manifest / manifest_counts)
# --------------------------------------------------------------------------------------------


def _city(
    slug, *, max_episodes=50, extract_audio=False, asr_enabled=True, asr_alignment_enabled=False
):
    return City(
        slug=slug,
        provider="granicus",
        source={},
        podcast_title="t",
        podcast_author="a",
        podcast_email="",
        podcast_description="d",
        max_episodes=max_episodes,
        extract_audio=extract_audio,
        asr_enabled=asr_enabled,
        asr_alignment_enabled=asr_alignment_enabled,
    )


def _rec(
    days_ago,
    *,
    hosted=False,
    body="City Council",
    provider_text=False,
    transcript_key=None,
    media_kind="hls",
):
    rec = {
        "published": (NOW - timedelta(days=days_ago)).isoformat(),
        "body": body,
        "media_kind": media_kind,
        "links": {"transcript": "https://x/t.vtt"} if provider_text else {},
        "audio": {"url": "https://x/a.m4a"} if hosted else {},
    }
    if transcript_key:
        rec["transcript"] = {"key": transcript_key, "basis": "source:s0"}
    return rec


def _provider_rec(days_ago, *, align_spec="align-new", transcript_spec=None, diarize_spec=None):
    rec = _rec(days_ago, hosted=True)
    rec["provider_transcript"] = {
        "candidate": {
            "key": "transcripts/s/u-provider-abc.vtt",
            "spec_hash": "abc",
            "format": "vtt",
            "synced": True,
            "align_spec_hash": align_spec,
        }
    }
    if diarize_spec is not None:
        rec["provider_transcript"]["candidate"]["diarize_spec_hash"] = diarize_spec
    if transcript_spec is not None:
        rec["transcript"] = {
            "key": f"transcripts/s/u-provider-align-{transcript_spec}.vtt",
            "spec_hash": transcript_spec,
            "basis": "served",
            "synced": True,
        }
    if diarize_spec is not None:
        rec["speakers"] = {
            "key": f"transcripts/s/u-provider-diarize-{diarize_spec}.speakers.json",
            "spec_hash": diarize_spec,
            "synced": True,
        }
    return rec


def _tx_items(items):
    return [it for it in items if "transcript" in it.work_class]


def test_build_manifest_audio_queued_vs_done():
    recs = {"u1": _rec(1, hosted=False, media_kind="hls"), "u2": _rec(2, hosted=True)}
    items = build_manifest([("s", _city("d"), recs)])
    audio = {it.episode_uid: it for it in items if it.work_class == "audio"}
    assert audio["u1"].state == "queued"
    assert audio["u2"].state == "done"


def test_build_manifest_direct_provider_no_host_no_audio_item():
    # A direct (non-HLS) episode with no hosting and extract_audio off → no audio work item.
    recs = {"u": _rec(1, hosted=False, media_kind="direct")}
    items = build_manifest([("s", _city("d", extract_audio=False), recs)])
    assert not [it for it in items if it.work_class == "audio"]


def test_build_manifest_transcript_asr_when_no_source_text():
    recs = {"u": _rec(1, hosted=True, provider_text=False)}
    tx = _tx_items(build_manifest([("s", _city("d"), recs)]))
    assert len(tx) == 1 and tx[0].work_class == "transcript-asr" and tx[0].state == "queued"


def test_build_manifest_alignment_disabled():
    recs = {"u": _rec(1, hosted=True, provider_text=True)}
    tx = _tx_items(build_manifest([("s", _city("d", asr_alignment_enabled=False), recs)]))
    assert tx[0].work_class == "transcript-align" and tx[0].state == "alignment-disabled"


def test_build_manifest_align_queued_when_enabled():
    recs = {"u": _rec(1, hosted=True, provider_text=True)}
    tx = _tx_items(build_manifest([("s", _city("d", asr_alignment_enabled=True), recs)]))
    assert tx[0].work_class == "transcript-align" and tx[0].state == "queued"


def test_build_manifest_provider_transcript_align_queued_for_timed_registry():
    recs = {"u": _provider_rec(1)}
    tx = _tx_items(build_manifest([("s", _city("d"), recs)]))
    assert tx[0].work_class == "provider-transcript-align"
    assert tx[0].state == "queued"


def test_build_manifest_provider_transcript_align_done_when_active_spec_matches():
    recs = {"u": _provider_rec(1, align_spec="align-new", transcript_spec="align-new")}
    tx = _tx_items(build_manifest([("s", _city("d"), recs)]))
    assert tx[0].work_class == "provider-transcript-align"
    assert tx[0].state == "done"


def test_build_manifest_provider_transcript_diarize_queued_after_align_selected():
    recs = {"u": _provider_rec(1, align_spec="align-new", transcript_spec="align-new")}
    tx = _tx_items(build_manifest([("s", _city("d"), recs)]))
    assert [(it.work_class, it.state) for it in tx] == [
        ("provider-transcript-align", "done"),
        ("provider-transcript-diarize", "queued"),
    ]


def test_build_manifest_provider_transcript_diarize_waits_for_selected_align():
    recs = {"u": _provider_rec(1, align_spec="align-new", transcript_spec="align-old")}
    tx = _tx_items(build_manifest([("s", _city("d"), recs)]))
    assert [(it.work_class, it.state) for it in tx] == [("provider-transcript-align", "queued")]


def test_build_manifest_provider_transcript_diarize_done_when_spec_matches():
    recs = {
        "u": _provider_rec(
            1,
            align_spec="align-new",
            transcript_spec="align-new",
            diarize_spec="speaker-new",
        )
    }
    tx = _tx_items(build_manifest([("s", _city("d"), recs)]))
    assert [(it.work_class, it.state) for it in tx] == [
        ("provider-transcript-align", "done"),
        ("provider-transcript-diarize", "done"),
    ]


def test_build_manifest_transcript_done():
    recs = {"u": _rec(1, hosted=True, transcript_key="k1")}
    tx = _tx_items(build_manifest([("s", _city("d"), recs)]))
    assert tx[0].state == "done"


def test_build_manifest_no_transcript_without_hosted_audio():
    recs = {"u": _rec(1, hosted=False)}
    assert not _tx_items(build_manifest([("s", _city("d"), recs)]))


def test_build_manifest_no_transcript_when_asr_disabled():
    recs = {"u": _rec(1, hosted=True)}
    assert not _tx_items(build_manifest([("s", _city("d", asr_enabled=False), recs)]))


def test_build_manifest_deep_archive_beyond_cap():
    recs = {f"u{i}": _rec(i, hosted=True, body="City Council") for i in range(5)}
    items = build_manifest([("s", _city("d", max_episodes=2), recs)])
    audio = {it.episode_uid: it for it in items if it.work_class == "audio"}
    assert audio["u0"].priority_bucket == BUCKET_FEED_VISIBLE
    assert audio["u1"].priority_bucket == BUCKET_FEED_VISIBLE
    assert audio["u2"].priority_bucket == BUCKET_DEEP_ARCHIVE
    assert audio["u4"].priority_bucket == BUCKET_DEEP_ARCHIVE


def test_manifest_counts():
    recs = {
        "u0": _rec(0, hosted=True, provider_text=True),  # audio done + align alignment-disabled
        "u1": _rec(1, hosted=False, media_kind="hls"),  # audio queued
        "u2": _rec(2, hosted=True, provider_text=False),  # audio done + transcript-asr queued
    }
    counts = manifest_counts(build_manifest([("s", _city("d", asr_alignment_enabled=False), recs)]))
    assert counts["by_work_class"]["audio"]["done"] == 2
    assert counts["by_work_class"]["audio"]["queued"] == 1
    assert counts["by_work_class"]["transcript-asr"]["queued"] == 1
    assert counts["by_work_class"]["transcript-align"]["alignment-disabled"] == 1
    assert counts["alignment_disabled"] == 1
    # queued only — alignment-disabled is NOT counted as actionable backlog
    assert counts["feed_visible_pending"] == 2
    assert counts["deep_archive_items"] == 0


def test_manifest_counts_deep_archive_excluded_from_actionable():
    recs = {f"u{i}": _rec(i, hosted=False, media_kind="hls") for i in range(4)}
    counts = manifest_counts(build_manifest([("s", _city("d", max_episodes=2), recs)]))
    assert counts["feed_visible_pending"] == 2  # only the newest 2 are actionable
    assert counts["deep_archive_items"] == 2


# --------------------------------------------------------------------------------------------
# Sidecar persistence + lease API
# --------------------------------------------------------------------------------------------


def test_manifest_save_load_round_trip(tmp_path):
    items = [
        WorkItem(
            "s",
            "u1",
            "audio",
            published=NOW,
            city_slug="d",
            body="City Council",
            priority_bucket=BUCKET_FEED_VISIBLE,
            state="queued",
        ),
        WorkItem("s", "u2", "transcript-align", published=None, state="alignment-disabled"),
    ]
    save_manifest(tmp_path, items)
    loaded = load_manifest(tmp_path)
    assert len(loaded) == 2
    assert loaded[0].episode_uid == "u1" and loaded[0].published == NOW
    assert loaded[0].priority_bucket == BUCKET_FEED_VISIBLE and loaded[0].state == "queued"
    assert loaded[1].published is None and loaded[1].state == "alignment-disabled"


def test_load_manifest_absent_returns_empty(tmp_path):
    assert load_manifest(tmp_path) == []


def test_lease_acquire_and_expire():
    wi = WorkItem("s", "u", "audio")
    lease(wi, "runner-1", ttl_seconds=300, now=NOW)
    assert wi.lease_owner == "runner-1" and wi.state == "running"
    assert is_leased(wi, now=NOW)
    assert is_leased(wi, now=NOW + timedelta(seconds=299))
    assert not is_leased(wi, now=NOW + timedelta(seconds=301))


def test_lease_release():
    wi = WorkItem("s", "u", "audio")
    lease(wi, "r", ttl_seconds=60, now=NOW)
    release(wi)
    assert wi.lease_owner == "" and wi.lease_expires is None
    assert not is_leased(wi, now=NOW)


def test_is_leased_false_when_unleased():
    assert not is_leased(WorkItem("s", "u", "audio"), now=NOW)


# --------------------------------------------------------------------------------------------
# order_cities_by_policy (light cross-source ordering)
# --------------------------------------------------------------------------------------------


def test_order_cities_by_policy_newest_first():
    a, b, c = _city("a-tx"), _city("b-tx"), _city("c-tx")
    recs_by_slug = {
        "a-tx": {"u": _rec(10, hosted=True)},
        "b-tx": {"u": _rec(1, hosted=True)},
        "c-tx": {"u": _rec(5, hosted=True)},
    }
    ordered = order_cities_by_policy([a, b, c], recs_by_slug, _policy({"recency": "desc"}))
    assert [c.slug for c in ordered] == ["b-tx", "c-tx", "a-tx"]


def test_order_cities_by_policy_normalizes_mixed_published_timestamps():
    a, b = _city("a-tx"), _city("b-tx")
    aware = _rec(1, hosted=True)
    naive = _rec(2, hosted=True)
    naive["published"] = (NOW - timedelta(days=2)).replace(tzinfo=None).isoformat()
    recs_by_slug = {
        "a-tx": {"aware": aware, "naive": naive},
        "b-tx": {"u": _rec(5, hosted=True)},
    }

    ordered = order_cities_by_policy([b, a], recs_by_slug, _policy({"recency": "desc"}))

    assert [c.slug for c in ordered] == ["a-tx", "b-tx"]


def test_order_cities_by_policy_identity_without_policy():
    a, b = _city("a-tx"), _city("b-tx")
    assert order_cities_by_policy([a, b], {}, None) == [a, b]
    assert order_cities_by_policy([a, b], {}, BacklogPolicy()) == [a, b]
