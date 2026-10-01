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
    spec = trend.VERBS[verb]
    return {
        "ts": datetime(2026, 9, day, hour, tzinfo=UTC).isoformat(),
        "phase": "enrich",
        "lane": spec.lane,
        "outcome": outcome,
        "stages": {spec.stage: {"ran": ran, "defer_reasons": reasons or {}, "backlog": 999}},
    }


def series(values, *, verb="chapter-locator", token="llm-pending", ran=1):
    return [
        event(24 + index, verb=verb, reasons={token: value}, ran=ran)
        for index, value in enumerate(values)
    ]


def test_golden_results_from_committed_fixture():
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
    report = trend.analyze(
        [event(verb="tagger", reasons={"tag-prelabeler-dispatch": 90})], "tagger", PARAMS
    )
    assert report.backlog == 0
    assert report.unclassified == {}


def test_unknown_token_is_reported_not_counted():
    events = [event(verb="tagger", reasons={"new-reason": 3, "tag-llm-dispatch": 2})]
    reports = trend.analyze_all(events, PARAMS)
    assert reports["tagger"].backlog == 2
    assert reports["tagger"].unclassified == {"new-reason": 3}
    assert trend._unclassified_tokens(reports) == {"tags": {"new-reason": 3}}


def test_blocked_policy_held_and_errored_do_not_count():
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
    events = [
        event(reasons={"llm-pending": 2}),
        event(reasons={"llm-pending": 999}, hour=5, outcome="interrupted"),
    ]
    point = trend.daily_points(events, "chapter-locator", PARAMS)[0]
    assert point.backlog == 2
    assert point.ran_day == 1


def test_insufficient_data_below_min_points():
    for events in ([], series([10, 100, 1000])):
        report = trend.analyze(events, "chapter-locator", PARAMS)
        assert report.trend == "insufficient_data"
        assert report.slope is report.mean is report.throughput_per_day is report.drain_days is None
        assert report.constrained is False
        assert report.action is None


def test_trend_deadband():
    for values, expected in (
        ([100, 101, 102, 103], "flat"),
        ([10, 11, 12, 13], "growing"),
        ([13, 12, 11, 10], "shrinking"),
    ):
        assert trend.analyze(series(values), "chapter-locator", PARAMS).trend == expected


def test_ols_slope_exact():
    report = trend.analyze(series([2, 4, 6, 8]), "chapter-locator", PARAMS)
    assert report.slope == 2
    assert report.mean == 5


def test_constrained_requires_drain_for_reliable_verbs():
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
    growing = series([10, 20, 30, 40], verb="tagger", token="tag-llm-dispatch", ran=1000)
    assert trend.analyze(growing, "tagger", PARAMS).constrained is True
    flat = series([40] * 4, verb="tagger", token="tag-llm-dispatch", ran=0)
    assert trend.analyze(flat, "tagger", PARAMS).constrained is False


def test_action_split_ingress_vs_route_capacity():
    for ingress, expected in ((19, "add_route_capacity"), (20, "raise_ingress_quota")):
        events = series([40] * 4, ran=1)
        for row in events:
            row["stages"]["chapter_locator"]["defer_reasons"] = {
                "llm-pending": 40 - ingress,
                "producer-cap": ingress,
            }
        assert trend.analyze(events, "chapter-locator", PARAMS).action == expected


def test_params_from_config_defaults_and_overrides():
    assert trend.params_from_config({}) == PARAMS
    assert trend.params_from_config({"llm_backlog": {"window_days": 3, "deadband_rel": 0.1}}) == (
        trend.BacklogParams(window_days=3, deadband_rel=0.1)
    )


def test_unknown_config_key_rejected():
    with pytest.raises(ValueError, match="Unknown.*typo"):
        trend.params_from_config({"llm_backlog": {"typo": 3}})


def test_main_reads_state_dir_and_writes_json_and_markdown(tmp_path, monkeypatch):
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
    assert trend.main(args) == 2
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
            listed.append(prefix)
            return [(key, NOW) for key in keys]

        def get_file(self, key, local_path):
            requested.append(key)
            if key == "state/catalog/manifest.json":
                return False
            local_path.parent.mkdir(parents=True, exist_ok=True)
            day = 21 if "boundary" in key else 30
            local_path.write_text(json.dumps(event(day, hour=12)))
            return True

    monkeypatch.setattr(storage, "make_storage", lambda *args: FakeStorage())
    output = tmp_path / "report.json"
    assert trend.main(["--site-config", str(config), "--json", str(output)]) == 0
    assert listed == ["state/run_events/"]
    assert sorted(key for key in requested if "/run_events/" in key) == keys[1:]
    assert json.loads(output.read_text())["skipped_files"] == 0
