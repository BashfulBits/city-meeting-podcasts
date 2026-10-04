"""Acceptance tests for review/50 PR1, including the frozen real run events."""

import json
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from citypods.ops import backlog_trend as trend

PARAMS = trend.BacklogParams()
NOW = datetime(2026, 9, 30, 12, tzinfo=UTC)


def event(day=30, *, verb="chapter-locator", reasons=None, ran=1, hour=0, outcome="completed"):
    """Build a scoped completed event with a deliberately unrelated aggregate backlog."""
    spec = trend.VERBS[verb]
    return {
        "ts": datetime(2026, 9, day, hour, tzinfo=UTC).isoformat(),
        "phase": "enrich",
        "lane": spec.lane,
        "outcome": outcome,
        "stages": {spec.stage: {"ran": ran, "defer_reasons": reasons or {}, "backlog": 999}},
    }


def series(values, *, verb="chapter-locator", token="llm-pending", ran=1):
    """Build one event per day for a hand-computed backlog trend."""
    return [
        event(24 + index, verb=verb, reasons={token: value}, ran=ran)
        for index, value in enumerate(values)
    ]


def test_golden_results_from_committed_fixture():
    """Reproduce every golden result from the unchanged real-event fixture."""
    fixture = Path(__file__).parent / "fixtures/backlog_trend/run_events_2026_09_22_30.json"
    reports = trend.analyze_all(json.loads(fixture.read_text())["events"], PARAMS)
    golden = {
        "chapter-agenda": (1625, -780.07, 4277.1, "shrinking", 713.9, 2.28, False, None),
        "chapter-locator": (556, 85.36, 356.1, "growing", 157.1, 3.54, False, None),
        "tagger": (25, -0.86, 23.7, "flat", 168.9, 0.15, False, None),
        "prelabeler": (68, -18.82, 121.6, "shrinking", 168.9, 0.40, False, None),
        "prelabeler-shadow": (
            122,
            18.21,
            86.1,
            "growing",
            168.9,
            0.72,
            True,
            "raise_ingress_quota",
        ),
        "moments": (17, -13.39, 37.1, "shrinking", 23.1, 0.73, False, None),
    }
    keys = (
        "backlog",
        "slope",
        "mean",
        "trend",
        "throughput_per_day",
        "drain_days",
        "constrained",
        "action",
    )
    for verb, expected in golden.items():
        report = asdict(reports[verb])
        assert report["days"] == 7
        for key, value in zip(keys, expected, strict=True):
            if isinstance(value, float):
                assert report[key] == pytest.approx(value, abs=0.005)
            else:
                assert report[key] == value
        assert report["unclassified"] == {}
    assert reports["prelabeler-shadow"].classes["ingress_limited"] == 122
    assert reports["moments"].blocked_or_held == {
        "blocked": 0,
        "policy_held": 1210,
        "errored": 1,
    }


def test_token_ownership_ignores_sibling_verb_tokens():
    """Keep prelabeler backlog out of the tagger report on the same stage."""
    report = trend.analyze(
        [event(verb="tagger", reasons={"tag-prelabeler-dispatch": 90})], "tagger", PARAMS
    )
    assert report.backlog == 0
    assert report.unclassified == {}


def test_unknown_token_is_reported_not_counted():
    """Expose unknown stage tokens once without adding them to working backlog."""
    events = [event(verb="tagger", reasons={"new-reason": 3, "tag-llm-dispatch": 2})]
    reports = trend.analyze_all(events, PARAMS)
    assert reports["tagger"].backlog == 2
    assert reports["tagger"].unclassified == {"new-reason": 3}
    assert trend._unclassified_tokens(reports) == {"tags": {"new-reason": 3}}


def test_blocked_policy_held_and_errored_do_not_count():
    """Report held and failed work separately and retain the future capacity token."""
    events = [
        event(verb="chapter-agenda", reasons={"missing-agenda-artifact": 5}),
        event(verb="moments", reasons={"rollout-dispatch-cap": 50, "llm-error": 2}),
    ]
    reports = trend.analyze_all(events, PARAMS)
    assert reports["chapter-agenda"].backlog == reports["moments"].backlog == 0
    assert reports["chapter-agenda"].blocked_or_held["blocked"] == 5
    assert reports["moments"].blocked_or_held["policy_held"] == 50
    assert reports["moments"].blocked_or_held["errored"] == 2
    assert (
        trend.analyze(
            [event(verb="moments", reasons={"llm-capacity": 7})], "moments", PARAMS
        ).backlog
        == 7
    )


def test_last_event_of_day_wins_and_ran_sums_all_events():
    """Verify daily snapshots, UTC grouping, throughput sums and window selection."""
    events = [
        event(reasons={"llm-pending": 10}, ran=3, hour=1),
        event(reasons={"producer-cap": 6}, ran=4, hour=2),
    ]
    point = trend.daily_points(list(reversed(events)), "chapter-locator", PARAMS)[0]
    assert point.backlog == 6
    assert point.ran_day == 7
    # UTC dates, including offset timestamps, and the last N populated dates define the window.
    offset = event(29, reasons={"llm-pending": 99})
    offset["ts"] = "2026-09-29T23:30:00-03:00"
    assert trend.daily_points([*events, offset], "chapter-locator", PARAMS)[0].backlog == 99
    assert trend.daily_points(events, "chapter-locator", PARAMS, through=date(2026, 9, 29)) == []
    points = trend.daily_points(
        series(range(7)), "chapter-locator", trend.BacklogParams(window_days=3)
    )
    assert [p.backlog for p in points] == [4, 5, 6]


def test_interrupted_events_are_ignored():
    """Exclude interrupted snapshots from both backlog and completed throughput."""
    events = [
        event(reasons={"llm-pending": 2}),
        event(reasons={"llm-pending": 999}, hour=5, outcome="interrupted"),
    ]
    point = trend.daily_points(events, "chapter-locator", PARAMS)[0]
    assert point.backlog == 2
    assert point.ran_day == 1


def test_insufficient_data_below_min_points():
    """Withhold trend judgements until enough distinct completed days exist."""
    for events in ([], series([10, 100, 1000])):
        report = trend.analyze(events, "chapter-locator", PARAMS)
        assert report.trend == "insufficient_data"
        assert report.slope is report.mean is report.throughput_per_day is report.drain_days is None
        assert report.constrained is False
        assert report.action is None


def test_trend_deadband():
    """Distinguish flat, growing and shrinking trends at the configured deadband."""
    for values, expected in (
        ([100, 101, 102, 103], "flat"),
        ([10, 11, 12, 13], "growing"),
        ([13, 12, 11, 10], "shrinking"),
    ):
        assert trend.analyze(series(values), "chapter-locator", PARAMS).trend == expected


def test_ols_slope_exact():
    """Verify the slope and mean of a hand-computed linear series."""
    report = trend.analyze(series([2, 4, 6, 8]), "chapter-locator", PARAMS)
    assert report.slope == 2
    assert report.mean == 5


def test_constrained_requires_drain_for_reliable_verbs():
    """Require long or unavailable drain time before flagging a reliable growing verb."""
    values = [10, 20, 30, 40]
    assert trend.analyze(series(values, ran=100), "chapter-locator", PARAMS).constrained is False
    assert trend.analyze(series(values, ran=1), "chapter-locator", PARAMS).constrained is True
    assert trend.analyze(series(values, ran=0), "chapter-locator", PARAMS).drain_days is None
    assert trend.analyze(series(values, ran=0), "chapter-locator", PARAMS).constrained is True
    assert (
        trend.analyze(series([40, 30, 20, 10], ran=0), "chapter-locator", PARAMS).constrained
        is False
    )


def test_unreliable_verbs_use_trend_only():
    """Preserve the spec's trend-only constraint rule for unreliable throughput."""
    growing = series([10, 20, 30, 40], verb="tagger", token="tag-llm-dispatch", ran=1000)
    assert trend.analyze(growing, "tagger", PARAMS).constrained is True
    flat = series([40] * 4, verb="tagger", token="tag-llm-dispatch", ran=0)
    assert trend.analyze(flat, "tagger", PARAMS).constrained is False


def test_action_split_ingress_vs_route_capacity():
    """Select ingress recommendations at the inclusive fifty-percent boundary."""
    for ingress, expected in ((19, "add_route_capacity"), (20, "raise_ingress_quota")):
        events = series([40] * 4, ran=1)
        for row in events:
            row["stages"]["chapter_locator"]["defer_reasons"] = {
                "llm-pending": 40 - ingress,
                "producer-cap": ingress,
            }
        assert trend.analyze(events, "chapter-locator", PARAMS).action == expected


def test_params_from_config_defaults_and_overrides():
    """Apply only explicitly overridden backlog settings."""
    assert trend.params_from_config({}) == PARAMS
    assert trend.params_from_config({"llm_backlog": {"window_days": 3, "deadband_rel": 0.1}}) == (
        trend.BacklogParams(window_days=3, deadband_rel=0.1)
    )


def test_unknown_config_key_rejected():
    """Reject misspelled configuration instead of silently ignoring it."""
    with pytest.raises(ValueError, match="Unknown.*typo"):
        trend.params_from_config({"llm_backlog": {"typo": 3}})


def test_main_reads_state_dir_and_writes_json_and_markdown(tmp_path, monkeypatch):
    """Verify local report files, skipped inputs, unknown reasons and all exit codes."""
    monkeypatch.setattr(trend, "_now", lambda: NOW)
    config = tmp_path / "site.yml"
    config.write_text("{}\n")
    directory = tmp_path / "run_events"
    directory.mkdir()
    output = tmp_path / "report.json"
    markdown = tmp_path / "report.md"
    args = [
        "--site-config",
        str(config),
        "--state-dir",
        str(tmp_path),
        "--json",
        str(output),
        "--markdown",
        str(markdown),
    ]
    assert trend.main(args) == 0
    assert trend.main([*args, "--fail-on-unclassified"]) == 2
    (directory / "good.json").write_text(json.dumps(event(reasons={"llm-pending": 5})))
    (directory / "old.json").write_text(json.dumps(event(1)))
    (directory / "broken.json").write_text("bad json")
    (directory / "array.json").write_text("[]")
    assert trend.main(args) == 0
    report = json.loads(output.read_text())
    assert report["skipped_files"] == 2
    assert report["verbs"]["chapter-locator"]["backlog"] == 5
    assert all(name in markdown.read_text() for name in trend.VERBS)
    assert len(trend.load_events(tmp_path, since=NOW - timedelta(days=9))) == 1
    (directory / "good.json").write_text(json.dumps(event(reasons={"unknown": 3})))
    assert trend.main(args) == 0
    assert trend.main([*args, "--fail-on-unclassified"]) == 1
    assert json.loads(output.read_text())["unclassified_tokens"] == {
        "chapter_locator": {"unknown": 3},
    }


def test_main_storage_path_uses_exact_keys(tmp_path, monkeypatch):
    """Read exact event keys even when the snapshot manifest excludes append-only events."""
    from citypods import storage

    monkeypatch.setattr(trend, "_now", lambda: NOW)
    config = tmp_path / "site.yml"
    config.write_text("{}\n")
    keys = [
        "state/run_events/20260921T115959-old.json",
        "state/run_events/20260921T120000-boundary.json",
        "state/run_events/20260930T000000-new.json",
    ]
    requested = []
    listed = []

    class FakeStorage:
        def list_objects(self, prefix):
            """Record the requested prefix and return the fake remote event listing."""
            listed.append(prefix)
            return [(key, NOW) for key in keys]

        def get_file(self, key, local_path):
            """A snapshot manifest must not be consulted for the listed append-only events."""
            requested.append(key)
            assert key in keys[1:]
            local_path.parent.mkdir(parents=True, exist_ok=True)
            day = 21 if "boundary" in key else 30
            local_path.write_text(json.dumps(event(day, hour=12)))
            return True

    monkeypatch.setattr(storage, "make_storage", lambda *args: FakeStorage())
    output = tmp_path / "report.json"
    assert trend.main(["--site-config", str(config), "--json", str(output)]) == 0
    assert listed == ["state/run_events/"]
    assert sorted(requested) == keys[1:]
    assert json.loads(output.read_text())["skipped_files"] == 0


@pytest.mark.parametrize(
    "verb,token",
    [
        ("tagger", "tag-llm-dispatch"),
        ("prelabeler", "tag-prelabeler-dispatch"),
        ("prelabeler-shadow", "tag-prelabeler-shadow-dispatch"),
        ("moments", "llm-pending"),
    ],
)
def test_growing_unreliable_verb_with_zero_latest_backlog(verb, token):
    """A cleared latest backlog must not crash recommendations for any report verb."""
    reports = trend.analyze_all(series([0, 50, 100, 0], verb=verb, token=token), PARAMS)
    report = reports[verb]
    assert report.backlog == 0
    assert report.slope == 5.0
    assert report.trend == "growing"
    assert report.constrained is True
    assert report.action is None
    assert set(reports) == set(trend.VERBS)


@pytest.mark.parametrize("failure", ["missing", "transient", "denied"])
def test_main_remote_event_download_failures(tmp_path, monkeypatch, failure):
    from botocore.exceptions import ClientError, ReadTimeoutError

    from citypods import storage

    monkeypatch.setattr(trend, "_now", lambda: NOW)
    config = tmp_path / "site.yml"
    config.write_text("{}\n")

    class FakeStorage:
        def list_objects(self, prefix):
            return [("state/run_events/20260930T000000-new.json", NOW)]

        def get_file(self, key, path):
            if failure == "missing":
                return False
            if failure == "transient":
                raise ReadTimeoutError(endpoint_url="https://storage.invalid")
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "GetObject")

    monkeypatch.setattr(storage, "make_storage", lambda *args: FakeStorage())
    output = tmp_path / "report.json"
    if failure == "denied":
        with pytest.raises(ClientError):
            trend.main(["--site-config", str(config), "--json", str(output)])
        assert not output.exists()
    else:
        assert trend.main(["--site-config", str(config), "--json", str(output)]) == 2
        assert json.loads(output.read_text())["skipped_files"] == 1


@pytest.mark.parametrize("shard", [1, True, [0, 2], {"index": 0, "count": 2}])
def test_non_string_shards_are_skipped_without_losing_valid_events(tmp_path, shard):
    malformed = tmp_path / "malformed.json"
    valid = tmp_path / "valid.json"
    malformed.write_text(json.dumps({**event(), "shard": shard}))
    valid.write_text(json.dumps({**event(), "shard": "0/2"}))
    rows, skipped = trend._read_events([malformed, valid], since=NOW - timedelta(days=1))
    assert skipped == 1
    assert len(rows) == 1
    assert rows[0]["shard"] == "0/2"


@pytest.mark.parametrize("shard", [" 0/2", "0/2 ", "+0/2", "0/-2", "0/0", "2/2"])
def test_malformed_shard_strings_are_skipped(tmp_path, shard):
    path = tmp_path / "event.json"
    path.write_text(json.dumps({**event(), "shard": shard}))
    rows, skipped = trend._read_events([path], since=NOW - timedelta(days=1))
    assert rows == []
    assert skipped == 1


def test_equivalent_shard_keys_are_canonicalized(tmp_path):
    paths = []
    for index, shard in enumerate(["00/02", "0/2"]):
        path = tmp_path / f"event-{index}.json"
        path.write_text(json.dumps({**event(), "shard": shard}))
        paths.append(path)
    rows, skipped = trend._read_events(paths, since=NOW - timedelta(days=1))
    assert skipped == 0
    assert [row["shard"] for row in rows] == ["0/2", "0/2"]


def test_registry_discovers_new_verbs_and_retirement_without_code_changes():
    rows = [
        {
            "ts": NOW.isoformat(),
            "lane": "new-task",
            "outcome": "completed",
            "stages": {"new_task": {"ran": 2, "defer_reasons": {"llm-pending": 7}}},
        }
    ]
    specs, tokens, lifecycle = trend.discover_verbs(rows, {"llm_lanes": {"new-task": {}}})
    report = trend.analyze(rows, "new-task", trend.BacklogParams(), specs=specs, tokens=tokens)
    assert report.backlog == 7
    assert lifecycle["new-task"] == "active"
    assert lifecycle["tagger"] == "retired"
    specs, tokens, lifecycle = trend.discover_verbs(rows, {"llm_lanes": {}})
    assert lifecycle["new-task:new_task"] == "unregistered"
    assert (
        trend.analyze(
            rows, "new-task:new_task", trend.BacklogParams(), specs=specs, tokens=tokens
        ).backlog
        == 7
    )


def test_registered_purpose_without_telemetry_is_visible():
    specs, _, lifecycle = trend.discover_verbs([], {"llm_lanes": {"new-shared-purpose": {}}})
    assert "new-shared-purpose" in specs
    assert lifecycle["new-shared-purpose"] == "no_telemetry"


def test_unknown_tokens_do_not_fail_scheduled_report_or_recommend_capacity(tmp_path, monkeypatch):
    monkeypatch.setattr(trend, "_now", lambda: NOW)
    config = tmp_path / "site.yml"
    config.write_text("llm_lanes:\n  chapter-agenda: {}\n")
    directory = tmp_path / "run_events"
    directory.mkdir()
    for day in range(24, 31):
        row = event(day, verb="chapter-agenda", reasons={"llm-pending": day * 10})
        row["stages"]["chapter_agenda"]["defer_reasons"]["new-token"] = 3
        (directory / f"{day}.json").write_text(json.dumps(row))
    output = tmp_path / "report.json"
    args = ["--site-config", str(config), "--state-dir", str(tmp_path), "--json", str(output)]
    assert trend.main(args) == 0
    report = json.loads(output.read_text())
    assert report["unclassified_tokens"]["chapter_agenda"] == {"new-token": 3}
    assert report["verbs"]["chapter-agenda"]["action"] is None
    assert report["verbs"]["chapter-agenda"]["constrained"] is False
    assert trend.main([*args, "--fail-on-unclassified"]) == 1


def test_distinct_lanes_with_same_stage_keep_token_ownership():
    rows = [
        {
            "ts": NOW.isoformat(),
            "lane": lane,
            "outcome": "completed",
            "stages": {"new_stage": {"defer_reasons": {"llm-pending": count}}},
        }
        for lane, count in [("lane-a", 3), ("lane-b", 8)]
    ]
    specs, tokens, lifecycle = trend.discover_verbs(rows, {"llm_lanes": {}})
    reports = {
        verb: trend.analyze(rows, verb, PARAMS, specs=specs, tokens=tokens)
        for verb in ["lane-a:new_stage", "lane-b:new_stage"]
    }
    assert reports["lane-a:new_stage"].backlog == 3
    assert reports["lane-b:new_stage"].backlog == 8
    assert lifecycle["lane-a:new_stage"] == "unregistered"


def test_multiple_stages_cannot_be_attributed_to_one_registry_purpose():
    rows = [
        {
            "ts": NOW.isoformat(),
            "lane": "new-task",
            "outcome": "completed",
            "stages": {
                stage: {"defer_reasons": {"llm-pending": count}}
                for stage, count in [("first", 3), ("second", 8)]
            },
        }
    ]
    specs, _, lifecycle = trend.discover_verbs(rows, {"llm_lanes": {"new-task": {}}})
    assert lifecycle["new-task"] == "no_telemetry"
    assert "new-task:first" in specs
    assert "new-task:second" in specs
