import json
from datetime import UTC, datetime

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
    assert doc["chapters"] == [{"start": 42.0, "title": "Parks budget"}]
    assert doc["segments"] == [{"start": 12.5, "text": "The council discussed the parks budget."}]
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
