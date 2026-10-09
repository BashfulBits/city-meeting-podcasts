import json
from datetime import UTC, datetime

import pytest

import citypods.search as search_mod
from citypods.availability import MISSING, MediaAvailability
from citypods.models import City, Episode
from citypods.records import episode_to_record, save_records, source_key
from citypods.search import build_search_index


class _Storage:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def get_file(self, key, path):
        self.calls.append(key)
        data = self.objects.get(key)
        if data is None:
            return False
        path.write_bytes(data)
        return True

    def public_url(self, key):
        return f"https://objects.test/{key}"


def _city(slug="austin-council", *, body=None):
    source = {"url": "https://example.test/archive"}
    if body:
        source["body"] = body
    return City(
        slug=slug,
        provider="faketest",
        source=source,
        podcast_title="Austin City Council",
        podcast_author="City of Austin, TX",
        podcast_email="",
        podcast_description="Council meetings",
        city_entity="austin-tx",
        state="TX",
    )


def _episode(uid="u1", *, transcript_key=None, withheld=False):
    return Episode(
        guid=f"guid-{uid}",
        uid=uid,
        title="City Council — Parks budget",
        body="City Council",
        published=datetime(2026, 7, 1, tzinfo=UTC),
        video_url="https://example.test/video/1",
        chapters=[{"start": 42, "title": "Parks budget"}],
        transcript_words_key=transcript_key,
        media_availability=MediaAvailability(state=MISSING, reason="no recording")
        if withheld
        else None,
    )


def _save(tmp_path, city, records):
    src = source_key(city)
    save_records(tmp_path / "state", src, records)
    return src


def _shard(tmp_path, src):
    return json.loads((tmp_path / "docs" / "data" / "search" / f"{src}.json").read_text())


def test_archive_only_search_removal_and_restoration_invalidate_cached_shards(tmp_path):
    city = _city()
    declaration = _city("archive-policy", body="Unrelated body")
    record = episode_to_record(_episode(transcript_key="one"))
    src = _save(tmp_path, city, {"u1": record})
    storage = _Storage(
        {"one": json.dumps({"segments": [{"start": 10, "text": "parks discussion"}]}).encode()}
    )
    cache = {}

    def build():
        return build_search_index(
            tmp_path / "state",
            [city, declaration],
            tmp_path / "docs",
            "https://site.test",
            storage=storage,
            cache=cache,
        )

    build()
    assert len(_shard(tmp_path, src)["documents"]) == 1
    initial_hash = cache["shards"][src]["hash"]
    declaration.extra["archive_only"] = [
        {
            "uid": "u1",
            "provider_guid": "guid-u1",
            "reason": "Human-approved archive disposition",
            "approval_ref": "https://example.test/approval/1",
        }
    ]
    storage.calls.clear()
    build()
    assert _shard(tmp_path, src)["documents"] == []
    assert cache["shards"][src]["hash"] != initial_hash
    assert storage.calls == []
    assert search_mod.load_records(tmp_path / "state", src) == {"u1": record}

    declaration.extra.pop("archive_only")
    build()
    assert len(_shard(tmp_path, src)["documents"]) == 1
    assert storage.calls == ["one"]
    assert cache["shards"][src]["hash"] == initial_hash


def test_archive_only_search_dispositions_do_not_cross_sources(tmp_path):
    archived = _city()
    public = _city("other-council")
    public.source["url"] = "https://other.test/archive"
    archived.extra["archive_only"] = [
        {
            "uid": "u1",
            "provider_guid": "guid-u1",
            "reason": "Human-approved archive disposition",
            "approval_ref": "https://example.test/approval/1",
        }
    ]
    record = episode_to_record(_episode())
    archived_src = _save(tmp_path, archived, {"u1": record})
    public_src = _save(tmp_path, public, {"u1": record})
    build_search_index(
        tmp_path / "state", [archived, public], tmp_path / "docs", "https://site.test"
    )
    assert _shard(tmp_path, archived_src)["documents"] == []
    assert [doc["uid"] for doc in _shard(tmp_path, public_src)["documents"]] == ["u1"]


def test_search_routing_uses_directional_body_matching():
    broad_label = _city("agenda", body="City Council Agenda Meetings")
    exact_label = _city("council", body="City Council")

    assert (
        search_mod._city_for_record([broad_label, exact_label], {"body": "City Council"})
        is exact_label
    )


def test_text_values_excludes_llm_tag_bookkeeping_fields():
    """An admitted LLM tag carries confidence/explanation/source alongside its taxonomy id.
    _text_values flattens tag dicts into the public search index's tags field/facet, so those
    bookkeeping fields must never leak a raw model sentence or a numeric confidence score into
    what's shipped to browsers -- only the taxonomy id (and other real content) should surface."""
    tag = {
        "id": "housing",
        "source": "llm",
        "confidence": 0.83,
        "explanation": "The council votes to approve rezoning for new housing.",
        "evidence": [{"where": "transcript", "quote": "rezoning"}],
    }
    values = search_mod._text_values([tag])
    assert values == ["housing"]


def test_static_search_index_contains_sidecars_without_duplicate_flattened_fields(tmp_path):
    city = _city()
    ep = _episode(transcript_key="transcript-key")
    ep.links = {
        "agenda": "https://example.test/agenda.pdf",
        "minutes": "https://example.test/minutes.pdf",
        # Initial R3 records stored only public sidecar URLs. Search must retain that history.
        "agenda_text_artifact": "https://objects.test/agenda-key",
        "minutes_text_artifact": "https://objects.test/minutes-key",
    }
    ep.agenda_backup_url = "https://objects.test/backup-key"  # old records stored URL without key
    ep.minutes_roster = [{"name": "Alex Rivera"}]
    ep.minutes_votes = [
        {"agenda_item": "Parks budget", "votes": [{"member": "Alex Rivera", "vote": "yes"}]}
    ]
    record = episode_to_record(ep)
    record["tags"] = ["parks", "budget"]  # R5 forward-compatible field
    src = _save(tmp_path, city, {"u1": record})
    storage = _Storage(
        {
            "agenda-key": b"Staff recommends funding a new park.",
            "backup-key": json.dumps(
                {
                    "text": "Park map",
                    "links": [{"label": "Staff report", "text": "Detailed park report"}],
                }
            ).encode(),
            "minutes-key": json.dumps({"text": "The motion passed unanimously."}).encode(),
            "transcript-key": json.dumps(
                {"segments": [{"start": 12.5, "text": "The council discussed the parks budget."}]}
            ).encode(),
        }
    )

    manifest = build_search_index(
        tmp_path / "state", [city], tmp_path / "docs", "https://site.test", storage=storage
    )
    doc = _shard(tmp_path, src)["documents"][0]
    assert manifest["shards"][0]["city"] == "austin-tx"
    assert manifest["shards"][0]["city_label"] == "City of Austin, TX"
    assert manifest["shards"][0]["bodies"] == ["City Council"]
    assert doc["city"] == "austin-tx"
    assert doc["tags"] == ["parks", "budget"]
    assert "funding a new park" in doc["agenda_text"].lower()
    assert "detailed park report" in doc["backup_text"].lower()
    assert "passed unanimously" in doc["minutes_text"].lower()
    assert "Alex Rivera" in doc["roster_text"] and "Parks budget" in doc["votes_text"]
    assert doc["chapters"][0]["start"] == 42.0
    assert doc["chapters"][0]["title"] == "Parks budget"
    assert doc["chapters"][0]["tags"] == []
    assert doc["segments"][0]["start"] == 12.5
    assert doc["segments"][0]["text"] == "The council discussed the parks budget."
    assert doc["segments"][0]["chapter_id"] is None
    assert {"transcript_text", "chapters_text", "link_labels_text", "tags_text"}.isdisjoint(doc)
    assert "backup-key" in storage.calls


def test_partial_transcript_search_discloses_coverage_without_hiding_available_segments(tmp_path):
    city = _city()
    first = _episode("u1", transcript_key="one")
    second = _episode("u2")
    second.title = "City Council — Zoning"
    src = _save(tmp_path, city, {"u1": episode_to_record(first), "u2": episode_to_record(second)})
    storage = _Storage(
        {"one": json.dumps({"segments": [{"start": 10, "text": "parks discussion"}]}).encode()}
    )

    manifest = build_search_index(
        tmp_path / "state", [city], tmp_path / "docs", "https://site.test", storage=storage
    )
    assert manifest["shards"][0]["transcript_coverage_pct"] == 50.0
    assert manifest["shards"][0]["transcript_episode_count"] == 1
    assert manifest["shards"][0]["episode_count"] == 2
    assert manifest["shards"][0]["body_coverage"]["City Council"] == {
        "episode_count": 2,
        "transcript_episode_count": 1,
    }
    documents = _shard(tmp_path, src)["documents"]
    assert any(doc["segments"] for doc in documents)
    assert any(not doc["segments"] for doc in documents)


def test_partial_transcript_search_caches_unchanged_shard(tmp_path):
    city = _city()
    ep = _episode(transcript_key="one", withheld=True)
    records = {"u1": episode_to_record(ep)}
    src = _save(tmp_path, city, records)
    storage = _Storage(
        {"one": json.dumps({"segments": [{"start": 10, "text": "parks discussion"}]}).encode()}
    )
    cache = {}

    first = build_search_index(
        tmp_path / "state",
        [city],
        tmp_path / "docs",
        "https://site.test",
        storage=storage,
        cache=cache,
    )
    calls = list(storage.calls)
    # Routine availability-probe timestamps do not alter the public search document, so they must
    # not defeat the sidecar-read cache.
    records["u1"]["media_availability"]["last_check"] = "2026-07-02T00:00:00+00:00"
    _save(tmp_path, city, records)
    second = build_search_index(
        tmp_path / "state",
        [city],
        tmp_path / "docs",
        "https://site.test",
        storage=storage,
        cache=cache,
    )
    assert first == second
    assert first["shards"][0]["transcript_episode_count"] == 1
    assert _shard(tmp_path, src)["documents"][0]["segments"]
    assert storage.calls == calls


def test_search_keeps_withheld_metadata_but_suppresses_aggregate_duplicates_and_prunes_stale(
    tmp_path,
):
    city = _city()
    withheld = _episode("withheld", withheld=True)
    duplicate = _episode("duplicate")
    duplicate.integrity = {"aggregate_suppressed": True}
    src = _save(
        tmp_path,
        city,
        {"withheld": episode_to_record(withheld), "duplicate": episode_to_record(duplicate)},
    )
    stale = tmp_path / "docs" / "data" / "search" / "retired.json"
    stale.parent.mkdir(parents=True)
    stale.write_text("{}")

    build_search_index(tmp_path / "state", [city], tmp_path / "docs", "https://site.test")
    documents = _shard(tmp_path, src)["documents"]
    assert [doc["uid"] for doc in documents] == ["withheld"]
    assert documents[0]["is_withheld"] is True
    assert documents[0]["media_availability_state"] == MISSING
    assert documents[0]["agenda_text"] is None
    assert not stale.exists()


def test_search_uses_body_specific_page_and_copies_vendored_license(tmp_path):
    council = _city("austin-council", body="City Council")
    aggregate = _city("austin-all")
    ep = _episode()
    src = _save(tmp_path, council, {"u1": episode_to_record(ep)})

    manifest = build_search_index(
        tmp_path / "state", [aggregate, council], tmp_path / "docs", "https://site.test"
    )
    doc = _shard(tmp_path, src)["documents"][0]
    assert doc["page_url"].endswith("/austin-council/u1/")
    assert manifest["shards"][0]["shard_gzip_bytes"] > 0
    assert (tmp_path / "docs" / "assets" / "LICENSES" / "minisearch-7.1.2.txt").exists()


def test_search_defers_without_publishing_a_partial_manifest(tmp_path):
    city = _city()
    _save(tmp_path, city, {"u1": episode_to_record(_episode())})
    complete = build_search_index(
        tmp_path / "state", [city], tmp_path / "docs", "https://site.test"
    )
    complete_manifest = (tmp_path / "docs" / "data" / "search" / "manifest.json").read_text()

    manifest = build_search_index(
        tmp_path / "state",
        [city],
        tmp_path / "docs",
        "https://site.test",
        stop=lambda: True,
    )

    assert manifest is None
    assert complete is not None
    manifest_path = tmp_path / "docs" / "data" / "search" / "manifest.json"
    assert manifest_path.read_text() == complete_manifest
    assert (tmp_path / "docs" / "assets" / "minisearch-7.1.2.js").exists()


def test_search_assets_fail_before_copying_when_a_vendored_file_is_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(search_mod, "SEARCH_LICENSE", "LICENSES/missing.txt")

    with pytest.raises(FileNotFoundError, match="missing vendored search asset"):
        search_mod._write_search_asset(tmp_path / "docs")

    assert not (tmp_path / "docs" / "assets" / "minisearch-7.1.2.js").exists()


@pytest.mark.parametrize("mixed", [False, True])
def test_search_render_from_retained_records_routes_exact_body_without_topic_capture(
    tmp_path, mixed
):
    owner = _city("udc")
    owner.source["body_exact"] = ["UDC Advisory Committee"]
    if mixed:
        owner.source["body_any"] = ["City Council"]
    combined = _city("all-meetings")
    committee = _episode("committee")
    committee.body = "UDC Advisory Committee"
    committee.title = committee.body
    open_house = _episode("open-house")
    open_house.body = "UDC Advisory Committee Open House"
    open_house.title = open_house.body
    records = {ep.uid: episode_to_record(ep) for ep in [committee, open_house]}
    assert search_mod._city_for_record([owner, combined], records[committee.uid]) is owner
    assert search_mod._city_for_record([owner, combined], records[open_house.uid]) is combined
    src = _save(tmp_path, combined, records)
    build_search_index(
        tmp_path / "state", [owner, combined], tmp_path / "docs", "https://site.test"
    )
    documents = {doc["uid"]: doc for doc in _shard(tmp_path, src)["documents"]}
    assert documents[committee.uid]["page_url"].startswith("https://site.test/udc/")
    assert documents[open_house.uid]["page_url"].startswith("https://site.test/all-meetings/")
    assert documents[committee.uid]["body"] == committee.body
    assert documents[open_house.uid]["body"] == open_house.body


def test_exact_body_rule_changes_invalidate_cached_search_routes(tmp_path):
    owner = _city("udc")
    owner.source["body_exact"] = ["UDC Advisory Committee"]
    combined = _city("all-meetings")
    episode = _episode("committee")
    episode.body = "UDC Advisory Committee"
    records = {episode.uid: episode_to_record(episode)}
    src = _save(tmp_path, combined, records)
    cache = {}
    build_search_index(
        tmp_path / "state", [owner, combined], tmp_path / "docs", "https://site.test", cache=cache
    )
    original_hash = cache["shards"][src]["hash"]
    assert _shard(tmp_path, src)["documents"][0]["page_url"].startswith("https://site.test/udc/")

    owner.source["body_exact"] = ["Another Advisory Committee"]
    build_search_index(
        tmp_path / "state", [owner, combined], tmp_path / "docs", "https://site.test", cache=cache
    )
    assert cache["shards"][src]["hash"] != original_hash
    assert _shard(tmp_path, src)["documents"][0]["page_url"].startswith(
        "https://site.test/all-meetings/"
    )


def test_search_hash_preserves_legacy_views_without_exact_body_rules():
    records = {"u1": episode_to_record(_episode())}
    # Captured before body_exact was added to the fingerprint: existing city shards stay cached.
    assert search_mod._shard_hash(records, [_city()], "https://site.test") == (
        "3a14ea22a0c8d498b1365182e7e6f14a451a50417fbb37019e3e6384232f300b"
    )


def _selection_group(city, records, *, search=True):
    from citypods.publication_selection import record_identity_fingerprint

    snapshot = {
        "url": "https://example.test/official-agenda",
        "retrieved_at": "2026-07-01T00:00:00+00:00",
        "content_hash": "a" * 64,
    }
    return {
        "version": 1,
        "groups": [
            {
                "id": "reviewed-meeting",
                "source_key": source_key(city),
                "identity_kind": "same_provider_guid",
                "identity_key": "official-guid",
                "members": [
                    {
                        "uid": uid,
                        "provider_guid": "official-guid",
                        "record_fingerprint": record_identity_fingerprint(source_key(city), record),
                    }
                    for uid, record in records.items()
                ],
                "preferred_uid": next(iter(records)),
                "evidence_refs": [snapshot],
                "approval_ref": "https://github.com/BashfulBits/city-meeting-podcasts/issues/1997",
                "exposure": {
                    "status": "both-published",
                    "artifacts": [snapshot],
                    "rationale": "Reviewed both existing public pages.",
                },
                "search": search,
                "date_resolution": None,
            }
        ],
    }


def _duplicate_records():
    episodes = [_episode("1" * 16), _episode("2" * 16)]
    for episode in episodes:
        episode.guid = "official-guid"
    return {ep.uid: episode_to_record(ep) for ep in episodes}


def test_search_selection_projects_winner_and_explicit_owner(tmp_path):
    owner = _city("reviewed-owner", body="City Council")
    fallback = _city("legacy-owner", body="City Council")
    records = _duplicate_records()
    owner.extra["publication_selection"] = _selection_group(owner, records)
    src = _save(tmp_path, owner, records)
    cache = {}
    build_search_index(
        tmp_path / "state", [fallback, owner], tmp_path / "docs", "https://site.test", cache=cache
    )
    documents = _shard(tmp_path, src)["documents"]
    assert [doc["uid"] for doc in documents] == ["1" * 16]
    assert documents[0]["page_url"].startswith("https://site.test/reviewed-owner/")
    assert records == _duplicate_records()


def test_search_selection_hold_preserves_all_outputs_and_cache(tmp_path):
    import copy

    owner = _city(body="City Council")
    records = _duplicate_records()
    owner.extra["publication_selection"] = _selection_group(owner, records)
    _save(tmp_path, owner, records)
    cache = {}
    build_search_index(
        tmp_path / "state", [owner], tmp_path / "docs", "https://site.test", cache=cache
    )
    before = {
        str(path.relative_to(tmp_path / "docs")): path.read_bytes()
        for path in (tmp_path / "docs").rglob("*")
        if path.is_file()
    }
    cached = copy.deepcopy(cache)
    records["2" * 16]["title"] = "Changed official observation"
    _save(tmp_path, owner, records)
    assert (
        build_search_index(
            tmp_path / "state", [owner], tmp_path / "docs", "https://site.test", cache=cache
        )
        is None
    )
    after = {
        str(path.relative_to(tmp_path / "docs")): path.read_bytes()
        for path in (tmp_path / "docs").rglob("*")
        if path.is_file()
    }
    assert after == before
    assert cache == cached
    first_output = tmp_path / "new-output"
    empty_cache = {}
    assert (
        build_search_index(
            tmp_path / "state", [owner], first_output, "https://site.test", cache=empty_cache
        )
        is None
    )
    assert not first_output.exists()
    assert empty_cache == {}


def test_search_selection_preflights_later_source_before_any_write(tmp_path):
    first = _city("first")
    later = _city("later", body="City Council")
    later.source = {**later.source, "url": "https://other.test/archive"}
    records = _duplicate_records()
    later.extra["publication_selection"] = _selection_group(later, records)
    _save(tmp_path, first, {"u1": episode_to_record(_episode())})
    # The configured later source has no archive: a proof hold must precede first-source writes.
    cache = {}
    assert (
        build_search_index(
            tmp_path / "state", [first, later], tmp_path / "docs", "https://site.test", cache=cache
        )
        is None
    )
    assert not (tmp_path / "docs").exists()
    assert cache == {}


def test_search_selection_policy_invalidates_only_configured_hash(tmp_path):
    from citypods.publication_selection import load_selection_index, select_search_publication

    owner = _city(body="City Council")
    records = _duplicate_records()
    owner.extra["publication_selection"] = _selection_group(owner, records)
    plan = select_search_publication(load_selection_index([owner]), source_key(owner), records)
    original = search_mod._shard_hash(records, [owner], "https://site.test", selection=plan)
    owner.extra["publication_selection"]["groups"][0]["preferred_uid"] = "2" * 16
    changed = select_search_publication(load_selection_index([owner]), source_key(owner), records)
    assert (
        search_mod._shard_hash(records, [owner], "https://site.test", selection=changed) != original
    )
    owner.extra["publication_selection"] = _selection_group(owner, records, search=False)
    feed_only = select_search_publication(load_selection_index([owner]), source_key(owner), records)
    assert feed_only.policy_hash is None
    assert search_mod._shard_hash(records, [owner], "https://site.test", selection=feed_only) == (
        search_mod._shard_hash(records, [owner], "https://site.test")
    )


def test_archive_identity_conflict_holds_index_before_any_output_write(tmp_path):
    """A later source conflict must not replace any part of the complete search index."""
    first, later = _city("first"), _city("later")
    later.source = {**later.source, "url": "https://other.test/archive"}
    record = episode_to_record(_episode())
    _save(tmp_path, first, {record["uid"]: record})
    _save(tmp_path, later, {record["uid"]: record})
    later.extra["archive_only"] = [
        {
            "uid": record["uid"],
            "provider_guid": "wrong",
            "reason": "hold",
            "approval_ref": "approval",
        }
    ]
    cache = {}
    assert (
        build_search_index(
            tmp_path / "state", [first, later], tmp_path / "docs", "https://site.test", cache=cache
        )
        is None
    )
    assert not (tmp_path / "docs").exists()
    assert cache == {}


def test_partial_source_resumes_and_only_changed_records_restart(tmp_path, monkeypatch):
    city = _city()
    records = {f"u{i}": episode_to_record(_episode(f"u{i}")) for i in range(5)}
    _save(tmp_path, city, records)
    cache = {}
    calls = []
    original = search_mod._record_to_document

    def convert(city, record, **kwargs):
        calls.append(record["uid"])
        return original(city, record, **kwargs)

    monkeypatch.setattr(search_mod, "_record_to_document", convert)

    def run(limit):
        start = len(calls)
        return build_search_index(
            tmp_path / "state",
            [city],
            tmp_path / "docs",
            "https://site.test",
            cache=cache,
            stop=lambda: len(calls) - start >= limit,
        )

    assert run(2) is None
    assert calls == ["u0", "u1"]
    # Simulate durable JSON restoration between jobs.
    cache = json.loads(json.dumps(cache))
    records["u0"]["title"] = "Changed title"
    _save(tmp_path, city, records)
    assert run(2) is None
    assert calls == ["u0", "u1", "u0", "u2"]
    assert run(2) is not None
    assert calls == ["u0", "u1", "u0", "u2", "u3", "u4"]
    assert len(cache["partials"][source_key(city)]["records"]) == 5
    shard = _shard(tmp_path, source_key(city))
    assert len(shard["documents"]) == 5
    assert next(d for d in shard["documents"] if d["uid"] == "u0")["title"] == "Changed title"


def test_partial_source_invalidates_on_view_and_archive_policy_changes(tmp_path, monkeypatch):
    city = _city()
    records = {f"u{i}": episode_to_record(_episode(f"u{i}")) for i in range(3)}
    _save(tmp_path, city, records)
    cache = {}
    calls = []
    original = search_mod._record_to_document

    def convert(city, record, **kwargs):
        calls.append(record["uid"])
        return original(city, record, **kwargs)

    monkeypatch.setattr(search_mod, "_record_to_document", convert)

    def run():
        start = len(calls)
        return build_search_index(
            tmp_path / "state",
            [city],
            tmp_path / "docs",
            "https://site.test",
            cache=cache,
            stop=lambda: len(calls) - start >= 1,
        )

    assert run() is None
    city.podcast_title = "Changed feed title"
    assert run() is None
    assert calls == ["u0", "u0"]
    monkeypatch.setattr(search_mod, "archive_policy_hash", lambda *_: "changed-archive-policy")
    assert run() is None
    assert calls == ["u0", "u0", "u0"]


def test_completed_source_reuses_records_after_changes_and_cache_hits(tmp_path, monkeypatch):
    city = _city()
    records = {f"u{i}": episode_to_record(_episode(f"u{i}")) for i in range(3)}
    _save(tmp_path, city, records)
    cache = {}
    calls = []
    original = search_mod._record_to_document

    def convert(city, record, **kwargs):
        calls.append(record["uid"])
        return original(city, record, **kwargs)

    monkeypatch.setattr(search_mod, "_record_to_document", convert)

    def run():
        return build_search_index(
            tmp_path / "state", [city], tmp_path / "docs", "https://site.test", cache=cache
        )

    assert run() is not None
    assert calls == ["u0", "u1", "u2"]
    cache = json.loads(json.dumps(cache))
    assert run() is not None  # A whole-source cache hit must retain the record cache.
    assert calls == ["u0", "u1", "u2"]
    cache = json.loads(json.dumps(cache))
    records["u1"]["title"] = "Updated meeting"
    records["u3"] = episode_to_record(_episode("u3"))
    del records["u0"]
    _save(tmp_path, city, records)
    assert run() is not None
    assert calls == ["u0", "u1", "u2", "u1", "u3"]
    assert set(cache["partials"][source_key(city)]["records"]) == {"u1", "u2", "u3"}
    incremental = _shard(tmp_path, source_key(city))
    build_search_index(tmp_path / "state", [city], tmp_path / "docs", "https://site.test", cache={})
    assert _shard(tmp_path, source_key(city)) == incremental
