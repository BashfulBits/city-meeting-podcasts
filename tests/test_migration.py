from datetime import UTC, date, datetime

from citypods.migration import compare_provider_migration
from citypods.models import City, Episode
from citypods.records import assign_uids


def _city(**kwargs):
    values = dict(
        slug="example-tx-council",
        provider="swagit",
        source={"list_url": "https://example.new.swagit.com/views/1"},
        podcast_title="Council",
        podcast_author="Example, TX",
        podcast_email="",
        podcast_description="Meetings",
        source_id="legacy-source",
    )
    values.update(kwargs)
    return City(**values)


def _episode(guid, day, *, body="City Council"):
    return Episode(
        guid=guid,
        title=f"Meeting {day}",
        published=datetime(2026, 7, day, 18, tzinfo=UTC),
        video_url=f"https://video.example/{guid}",
        body=body,
    )


def test_copied_history_migration_preserves_matches_and_appends_new():
    city = _city()
    old = _episode("old-provider", 1)
    copied = _episode("replacement-copy", 1)
    new = _episode("replacement-new", 15)
    assign_uids(city, [old])
    assign_uids(city, [copied, new])
    archive = {old.uid: {"uid": old.uid, "audio": {"key": "kept.m4a"}}}

    report = compare_provider_migration(city, [copied, new], archive, cutover=date(2026, 7, 10))

    assert report.ready
    assert report.mode == "copied-history"
    assert [item.guid for item in report.matched_history] == ["replacement-copy"]
    assert [item.guid for item in report.new_episodes] == ["replacement-new"]
    assert report.projected_count == 2
    assert archive[old.uid]["audio"]["key"] == "kept.m4a"


def test_forward_only_migration_retains_archive_and_appends_candidate():
    city = _city()
    old = _episode("old-provider", 1)
    new = _episode("replacement-new", 15)
    assign_uids(city, [old])
    assign_uids(city, [new])

    report = compare_provider_migration(
        city, [new], {old.uid: {"uid": old.uid}}, cutover=date(2026, 7, 10)
    )

    assert report.ready
    assert report.mode == "forward-only"
    assert report.projected_count == 2
    assert not report.matched_history


def test_unmatched_pre_cutover_history_fails_closed_until_overridden():
    candidate = _episode("replacement-renamed", 1, body="Council Meeting")
    city = _city()
    assign_uids(city, [candidate])
    archive = {"0123456789abcdef": {"uid": "0123456789abcdef"}}
    report = compare_provider_migration(city, [candidate], archive, cutover=date(2026, 7, 10))
    assert not report.ready
    assert [item.guid for item in report.ambiguous_history] == ["replacement-renamed"]

    city.uid_overrides = {"replacement-renamed": "0123456789abcdef"}
    assign_uids(city, [candidate])
    resolved = compare_provider_migration(city, [candidate], archive, cutover=date(2026, 7, 10))
    assert resolved.ready
    assert [item.guid for item in resolved.overrides_applied] == ["replacement-renamed"]


def test_override_must_target_archive_and_present_candidate_guid():
    city = _city(uid_overrides={"missing-guid": "0123456789abcdef"})
    candidate = _episode("new", 15)
    assign_uids(city, [candidate])

    report = compare_provider_migration(city, [candidate], {}, cutover=date(2026, 7, 10))

    assert not report.ready
    assert len(report.invalid_overrides) == 2
