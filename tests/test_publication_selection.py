"""Frozen proof and failure behavior for inactive publication selection."""

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

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


@pytest.mark.parametrize("field", ["retrieved_at", "official_date"])
def test_yaml_native_dates_request_quoted_strings(field):
    import yaml

    city, _, group = packet()
    if field == "retrieved_at":
        group["evidence_refs"][0][field] = yaml.safe_load("2026-01-02T00:00:00Z")
    else:
        group["date_resolution"] = {
            field: yaml.safe_load("2026-01-01"),
            "evidence_url": group["evidence_refs"][0]["url"],
        }
    with pytest.raises(ValueError, match="quote ISO date/timestamp"):
        parse_publication_selection(
            city.extra["publication_selection"], source_key=source_key(city), feed_slug=city.slug
        )


@pytest.mark.parametrize(
    "field,value", [("retrieved_at", "not-a-timestamp"), ("official_date", "2026-02-30")]
)
def test_invalid_iso_values_retain_group_context(field, value):
    city, _, group = packet()
    if field == "retrieved_at":
        group["evidence_refs"][0][field] = value
    else:
        group["date_resolution"] = {field: value, "evidence_url": group["evidence_refs"][0]["url"]}
    with pytest.raises(ValueError, match=f"test: publication_selection.*{field}") as exc:
        parse_publication_selection(
            city.extra["publication_selection"], source_key=source_key(city), feed_slug=city.slug
        )
    assert isinstance(exc.value.__cause__, ValueError)


@pytest.mark.parametrize("reverse", [False, True])
def test_identical_feed_groups_allow_one_search_owner_in_any_order(reverse):
    owner, records, _ = packet()
    aggregate = replace(owner, slug="aggregate", extra=copy.deepcopy(owner.extra))
    aggregate.extra["publication_selection"]["groups"][0]["search"] = False
    cities = [aggregate, owner]
    if reverse:
        cities.reverse()
    index = load_selection_index(cities)
    for city in cities:
        assert select_feed_publication(index, city, records.values(), records).selected_uids == (
            "a" * 16,
        )
    plan = select_search_publication(index, source_key(owner), records)
    assert not plan.held and plan.owner_by_uid["a" * 16] is owner
    competing = replace(owner, slug="competing")
    with pytest.raises(ValueError, match="conflicting"):
        load_selection_index([owner, aggregate, competing])


FOUNDATION_WINNERS = frozenset({"52ea444ed933464a", "c73dbd3dab9b14cc", "ccee5be42508e88d"})
FOUNDATION_ALTERNATES = frozenset({"a54b5fc602d9e5c7", "85e716fc0a89c65a", "7d87cd30071ce800"})


def _foundation_activation_packet():
    from pathlib import Path

    from citypods.config import load_city_configs

    root = Path(__file__).resolve().parents[1]
    fixture = json.loads(
        (root / "tests/fixtures/arlington-foundation-publication.json").read_text()
    )
    cities = load_city_configs(root / "config", {})
    foundation = next(c for c in cities if c.slug == "arlington-tx-arlington-tomorrow-foundation")
    combined = next(c for c in cities if c.slug == "arlington-tx")
    return fixture, cities, foundation, combined


def test_foundation_approved_winners_render_record_backed_audio_and_video_rss():
    from xml.etree import ElementTree

    from citypods.bodies import filter_by_body, source_body_filter
    from citypods.feeds import build_rss
    from citypods.records import record_to_episode

    fixture, cities, foundation, combined = _foundation_activation_packet()
    records = fixture["records"]
    before = copy.deepcopy(records)
    index = load_selection_index(cities)
    episodes = [record_to_episode(r) for r in records.values()]
    for city in (foundation, combined):
        items = filter_by_body(episodes, source_body_filter(city.source))
        plan = select_feed_publication(index, city, items, records)
        assert not plan.held, plan.diagnostics
        assert set(plan.suppressed_uids) == FOUNDATION_ALTERNATES
        assert source_key(city) == fixture["source_key"]
        for kind in ("audio", "video"):
            rss = ElementTree.fromstring(
                build_rss(city, list(plan.public_items), kind, "https://www.citymeetings.fyi")
            )
            rss_items = rss.findall("channel/item")
            uids = [item.findtext("guid") for item in rss_items]
            assert len(uids) == len(set(uids))
            assert set(uids) & (FOUNDATION_WINNERS | FOUNDATION_ALTERNATES) == FOUNDATION_WINNERS
            if city.slug == foundation.slug:
                assert set(uids) == FOUNDATION_WINNERS
            for item in rss_items:
                uid = item.findtext("guid")
                if uid not in FOUNDATION_WINNERS:
                    continue
                enclosure = item.find("enclosure")
                assert enclosure is not None
                if kind == "audio":
                    assert enclosure.attrib["url"] == records[uid]["audio"]["url"]
    assert records == before
    assert all(records[uid]["uid"] == uid for uid in FOUNDATION_WINNERS | FOUNDATION_ALTERNATES)
    for group in foundation.extra["publication_selection"]["groups"]:
        assert group["preferred_uid"] in FOUNDATION_WINNERS
        for member in group["members"]:
            assert (
                record_identity_fingerprint(fixture["source_key"], records[member["uid"]])
                == (member["record_fingerprint"])
            )


def test_foundation_shared_groups_have_one_search_owner_and_preserve_other_feeds():
    from citypods.bodies import record_matches_body, source_body_filter, source_body_inclusions

    fixture, cities, foundation, combined = _foundation_activation_packet()
    records = fixture["records"]
    for declared in ([foundation, combined], [combined, foundation]):
        index = load_selection_index(declared)
        plan = select_search_publication(index, fixture["source_key"], records)
        assert not plan.held, plan.diagnostics
        assert set(plan.suppressed_uids) == FOUNDATION_ALTERNATES
        assert set(plan.owner_by_uid) == FOUNDATION_WINNERS
        assert {owner.slug for owner in plan.owner_by_uid.values()} == {foundation.slug}
    index = load_selection_index(cities)
    for city in cities:
        if city.slug not in fixture["unrelated_feed_controls"]:
            continue
        items = [
            r
            for r in records.values()
            if record_matches_body(
                r, source_body_filter(city.source), source_body_inclusions(city.source)
            )
        ]
        assert fixture["unrelated_feed_controls"][city.slug] in {r["uid"] for r in items}
        plan = select_feed_publication(index, city, items, records)
        assert not plan.held
        assert plan.public_items == tuple(items)
        assert not plan.suppressed_uids


def test_foundation_unreviewed_extra_same_guid_member_holds_full_source():
    fixture, cities, foundation, combined = _foundation_activation_packet()
    records = copy.deepcopy(fixture["records"])
    extra = copy.deepcopy(records["52ea444ed933464a"])
    extra["uid"] = "f" * 16
    # Same source/GUID is insufficient to authorize a new publication member.
    records[extra["uid"]] = extra
    index = load_selection_index(cities)
    # Extra observation is deliberately outside the body-selected item list. Full source proof
    # must still detect it before publication, including search.
    items = [fixture["records"][uid] for uid in FOUNDATION_WINNERS | FOUNDATION_ALTERNATES]
    for city in (foundation, combined):
        plan = select_feed_publication(index, city, items, records)
        assert plan.held
        assert "unexpected-member" in {d.code for d in plan.diagnostics}
    assert select_search_publication(index, fixture["source_key"], records).held


ADDISON_ROOT = Path(__file__).resolve().parents[1]
ADDISON_CPC_FIXTURE = ADDISON_ROOT / "tests/fixtures/addison-cpc-retained.json"
ADDISON_CPC_WINNER = "91efc2425e16e017"
ADDISON_CPC_DUPLICATE = "c70591ce9d69600c"
ADDISON_CPC_SOURCE = "abbf5e25e078"


def _addison_cpc_inputs():
    import json

    from citypods.config import load_city_configs

    cities = load_city_configs(ADDISON_ROOT / "config", {})
    committee = next(c for c in cities if c.slug == "addison-tx-community-partnership-committee")
    records = json.loads(ADDISON_CPC_FIXTURE.read_text())["records"]
    return cities, committee, records


def test_addison_cpc_five_rss_recordings_preserve_six_retained_observations():
    import defusedxml.ElementTree as DET

    from citypods import bodies
    from citypods.feeds import build_rss
    from citypods.records import record_to_episode, source_key

    cities, committee, records = _addison_cpc_inputs()
    original = copy.deepcopy(records)
    index = load_selection_index(cities)
    assert source_key(committee) == ADDISON_CPC_SOURCE
    group = next(g for g in index.groups if g.feed_slug == committee.slug)
    assert group.preferred_uid == ADDISON_CPC_WINNER
    assert group.date_resolution["official_date"] == "2026-07-09"
    assert group.exposure["status"] == "historical-unknown"
    for member in group.members:
        assert (
            record_identity_fingerprint(ADDISON_CPC_SOURCE, records[member.uid])
            == member.record_fingerprint
        )
    items = [record_to_episode(r) for r in records.values()]
    assert all(
        bodies.record_matches_body(
            r,
            bodies.source_body_filter(committee.source),
            bodies.source_body_inclusions(committee.source),
        )
        for r in records.values()
    )
    plan = select_feed_publication(index, committee, items, records)
    assert not plan.held
    assert len(plan.raw_items) == 6
    assert len(plan.public_items) == 5
    assert (
        ADDISON_CPC_WINNER in plan.selected_uids and ADDISON_CPC_DUPLICATE not in plan.selected_uids
    )
    xml = build_rss(committee, list(plan.public_items), "audio", "https://www.citymeetings.fyi")
    rss_items = DET.fromstring(xml).find("channel").findall("item")
    assert len(rss_items) == 5
    published_uids = {i.find("guid").text for i in rss_items}
    assert published_uids == set(records) - {ADDISON_CPC_DUPLICATE}
    winner_item = next(i for i in rss_items if i.find("guid").text == ADDISON_CPC_WINNER)
    assert winner_item.find("enclosure").get("url") == records[ADDISON_CPC_WINNER]["audio"]["url"]
    assert records == original
    assert len({r["provider_guid"] for r in records.values()}) == 5
    second = select_feed_publication(index, committee, items, records)
    assert second.selected_uids == plan.selected_uids
    assert second.policy_hash == plan.policy_hash


def test_addison_cpc_search_owner_and_other_feed_selectors():
    from citypods import bodies
    from citypods.records import source_key

    cities, committee, records = _addison_cpc_inputs()
    index = load_selection_index(cities)
    search = select_search_publication(index, ADDISON_CPC_SOURCE, records)
    assert not search.held
    assert len(search.public_items) == 5
    assert search.owner_by_uid[ADDISON_CPC_WINNER].slug == committee.slug
    for city in cities:
        if city.city_entity != "addison-tx" or city.slug == committee.slug:
            continue
        assert source_key(city) == ADDISON_CPC_SOURCE
        assert not any(
            bodies.record_matches_body(
                r,
                bodies.source_body_filter(city.source),
                bodies.source_body_inclusions(city.source),
            )
            for r in records.values()
        )
        assert select_feed_publication(index, city, [], records).policy_hash is None
    selector = bodies.source_body_filter(committee.source)
    assert not bodies.matches("Community Partnership Staff Training", selector)
    assert not bodies.matches("Other City Community Partnership Committee", selector)


@pytest.mark.parametrize(
    "mutation, diagnostic",
    [
        ("missing-winner", "missing-preferred"),
        ("changed-fingerprint", "fingerprint-changed"),
        ("new-member", "unexpected-member"),
    ],
)
def test_addison_cpc_changed_identity_holds_before_publication(mutation, diagnostic):
    from citypods.records import record_to_episode

    cities, committee, records = _addison_cpc_inputs()
    if mutation == "missing-winner":
        records.pop(ADDISON_CPC_WINNER)
    elif mutation == "changed-fingerprint":
        records[ADDISON_CPC_WINNER]["title"] += " changed"
    else:
        records["d" * 16] = {**records[ADDISON_CPC_WINNER], "uid": "d" * 16, "body": "Not selected"}
    plan = select_feed_publication(
        load_selection_index(cities),
        committee,
        [record_to_episode(r) for r in records.values()],
        records,
    )
    assert plan.held
    assert diagnostic in {d.code for d in plan.diagnostics}
    assert plan.public_items == plan.raw_items


def test_addison_cpc_new_unique_recording_classifies_without_identity_merge():
    from citypods import bodies
    from citypods.records import record_to_episode

    cities, committee, records = _addison_cpc_inputs()
    uid = "e" * 16
    records[uid] = {
        **copy.deepcopy(records[ADDISON_CPC_WINNER]),
        "uid": uid,
        "provider_guid": "999999",
        "video_url": "https://addisontx.new.swagit.com/videos/999999/download",
        "title": "Community Partnership Committee – Jul 23, 2026",
        "published": "2026-07-23T00:00:00+00:00",
    }
    records[uid].pop("audio")
    selector = bodies.source_body_filter(committee.source)
    assert bodies.matches("Community Partnership Committee", selector)
    assert bodies.matches("COMMUNITY PARTNERSHIP COMMITTEE", selector)
    assert not bodies.matches("Community Partnership Committee Open House", selector)
    assert not bodies.matches("Comprehensive Plan Advisory Committee", selector)
    original = copy.deepcopy(records)
    plan = select_feed_publication(
        load_selection_index(cities),
        committee,
        [record_to_episode(r) for r in records.values()],
        records,
    )
    assert not plan.held
    assert uid in plan.selected_uids
    assert next(item for item in plan.public_items if item.uid == uid).audio_url is None
    assert len(plan.public_items) == 6
    assert records == original


def test_addison_public_input_migration_preserves_historical_recording_and_routes_briefing():
    """Approved feed migration preserves Citizen Advisory identity and hosted audio."""
    import defusedxml.ElementTree as DET

    from citypods import bodies
    from citypods.config import load_city_configs
    from citypods.feeds import build_rss
    from citypods.records import record_to_episode, source_key

    cities = load_city_configs(ADDISON_ROOT / "config", {})
    public_input = next(c for c in cities if c.slug == "addison-tx-town-meetings")
    briefings = next(c for c in cities if c.slug == "addison-tx-public-briefings")
    records = json.loads(
        (ADDISON_ROOT / "tests/fixtures/addison-public-input-retained.json").read_text()
    )["records"]
    original = copy.deepcopy(records)
    index = load_selection_index(cities)
    assert public_input.podcast_title == "Addison: Public Input"
    assert source_key(public_input) == source_key(briefings) == ADDISON_CPC_SOURCE
    assert public_input.extra["meeting_family"] == "public_input"
    assert briefings.extra["meeting_family"] == "public_briefings"

    def selected(city):
        return {
            uid: r
            for uid, r in records.items()
            if bodies.record_matches_body(
                r,
                bodies.source_body_filter(city.source),
                bodies.source_body_inclusions(city.source),
            )
        }

    citizen_uid = "63b22f80ce9500ff"
    seminar_uid = "817ef9212ccc9ca0"
    input_records = selected(public_input)
    assert citizen_uid in input_records
    assert seminar_uid not in input_records
    assert set(selected(briefings)) == {seminar_uid}
    excluded_guids = {"56020", "56021", "56022", "56024", "56025"}
    assert not any(r["provider_guid"] in excluded_guids for r in input_records.values())
    # These older media cases remain visible for investigation, rather than silently disappearing.
    assert {"56026", "56027", "56028"} <= {r["provider_guid"] for r in input_records.values()}
    for city in (public_input, briefings):
        filtered = selected(city)
        plan = select_feed_publication(
            index, city, [record_to_episode(r) for r in filtered.values()], records
        )
        assert not plan.held
        xml = build_rss(city, list(plan.public_items), "audio", "https://www.citymeetings.fyi")
        channel = DET.fromstring(xml).find("channel")
        assert channel.find("title").text == f"{city.podcast_title} (Audio)"
        rss_items = {i.find("guid").text: i for i in channel.findall("item")}
        assert set(rss_items) == set(filtered)
        uid = citizen_uid if city.slug == public_input.slug else seminar_uid
        assert rss_items[uid].find("title").text == records[uid]["title"]
        assert rss_items[uid].find("enclosure").get("url") == records[uid]["audio"]["url"]
    selector = bodies.source_body_filter(briefings.source)
    assert bodies.matches("HOMELESSNESS EDUCATION SEMINAR", selector)
    assert not bodies.matches("Other City Homelessness Education Seminar", selector)
    assert not bodies.matches("Homelessness Education Seminar Staff Training", selector)
    assert records == original


def test_addison_appeals_display_preserves_combined_institution_source():
    """A formal dual-function board keeps its existing URL and both subscriptions."""
    from citypods import bodies
    from citypods.config import load_city_configs
    from citypods.records import source_key

    city = next(
        c
        for c in load_city_configs(ADDISON_ROOT / "config", {})
        if c.slug == "addison-tx-board-of-zoning-adjustment"
    )
    assert city.podcast_title == "Addison: Zoning Adjustment & Appeals"
    assert source_key(city) == ADDISON_CPC_SOURCE
    selector = bodies.source_body_filter(city.source)
    assert bodies.matches("Board of Zoning Adjustment", selector)
    assert bodies.matches("Board of Appeals", selector)
    assert not bodies.matches("Community Partnership Committee", selector)
