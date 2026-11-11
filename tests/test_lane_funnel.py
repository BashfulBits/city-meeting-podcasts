"""`/admin/status` LLM lane table: classes from funnel fields, old events, rendering."""

import json
import re
from datetime import UTC, datetime, timedelta

from citypods.compute.llm_lanes import load_lanes
from citypods.ops.lane_funnel import NOT_MEASURED_DISPATCH, classify, lane_rows
from citypods.report import build_status, to_status_html

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def purpose_row(states, **extra):
    return {
        "unit": "episode",
        "coverage": "complete",
        "observed": sum(states.values()),
        "states": states,
        **extra,
    }


def event(hours_ago, purpose, row):
    ts = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {"ts": ts, "llm_work": {"schema_version": 1, "purposes": {purpose: row}}}


def test_input_starved_waits_on_upstream():
    code, text = classify("chapter-agenda", purpose_row({"blocked": 6135, "consumed": 6841}))
    assert code == "a" and "waiting on agenda text" in text and "6135" in text
    code, text = classify("r6-moments", purpose_row({"blocked": 4218, "ready": 145, "queued": 6}))
    assert code == "a" or "waiting on transcripts" in text
    assert "waiting on transcripts" in text and "eligible backlog 151" in text


def test_producer_limited_when_ready_work_is_not_submitted():
    code, text = classify("chapter-locator", purpose_row({"ready": 1723, "queued": 274}))
    assert code == "b" and "1723 eligible" in text and "not submitted" in text


def test_budget_capped_when_ingress_refuses():
    code, text = classify("topic-tags:tagger", purpose_row({"ingress_limited": 700, "queued": 300}))
    assert code == "e" and "ingress budget closed" in text


def test_dispatcher_vs_return_path_need_the_poll_split():
    waiting = purpose_row(
        {"queued": 100},
        queue={"waiting_at_worker": 80, "completed_uncollected": 20, "unobserved": 0},
    )
    assert classify("judge:sibling", waiting)[0] == "c"
    done = purpose_row(
        {"queued": 100},
        queue={"waiting_at_worker": 10, "completed_uncollected": 90, "unobserved": 0},
    )
    code, text = classify("judge:sibling", done)
    assert code == "d" and "90 completed at the Worker, not collected" in text
    unpolled = purpose_row(
        {"queued": 100},
        queue={"waiting_at_worker": 0, "completed_uncollected": 0, "unobserved": 100},
    )
    code, text = classify("judge:sibling", unpolled)
    assert code == "unknown" and "not measured" in text


def test_old_event_without_funnel_fields_is_not_misclassified():
    code, text = classify("judge:anchor", purpose_row({"queued": 5}))
    assert code == "unknown" and "not measured" in text
    assert classify("judge:anchor", purpose_row({"consumed": 3}))[0] == "idle"
    assert classify("judge:anchor", {"states": {}, "observed": 0})[0] == "unknown"
    # Hostile or malformed values never raise.
    assert classify("judge:anchor", {"states": {"queued": "x"}, "observed": True})[0] == "unknown"


def test_row_reports_oldest_backlog_from_consecutive_events():
    history = [
        event(30, "chapter-agenda", purpose_row({"queued": 5})),
        event(20, "chapter-agenda", purpose_row({"queued": 4})),
        event(10, "chapter-agenda", purpose_row({"queued": 3})),
    ]
    (row,) = lane_rows(history, ["chapter-agenda"], now=NOW)
    assert row["backlog"] == 3
    assert "backlog seen for at least 30 h" in row["summary"]
    assert row["slot_productivity"] == NOT_MEASURED_DISPATCH
    assert row["failed_attempt_share"] == NOT_MEASURED_DISPATCH


def test_backlog_streak_stops_at_a_drained_run():
    history = [
        event(40, "chapter-agenda", purpose_row({"queued": 9})),
        event(30, "chapter-agenda", purpose_row({"consumed": 9})),
        event(5, "chapter-agenda", purpose_row({"queued": 1})),
    ]
    (row,) = lane_rows(history, ["chapter-agenda"], now=NOW)
    assert "at least 5 h" in row["summary"]


def test_first_seen_extends_the_streak_when_present():
    older = (NOW - timedelta(hours=100)).isoformat()
    history = [event(2, "chapter-agenda", purpose_row({"queued": 1}, first_seen_at=older))]
    (row,) = lane_rows(history, ["chapter-agenda"], now=NOW)
    assert "at least 4 d" in row["summary"]


def test_every_configured_lane_gets_a_row_even_with_no_events():
    purposes = sorted(load_lanes())
    rows = lane_rows([], purposes, now=NOW)
    assert [r["purpose"] for r in rows] == purposes
    assert all(r["class"] == "unknown" and "not measured" in r["summary"] for r in rows)


def test_newest_event_wins_and_other_purposes_are_ignored():
    history = [
        event(9, "judge:anchor", purpose_row({"queued": 50})),
        event(1, "judge:anchor", purpose_row({"consumed": 50})),
        {"ts": NOW.isoformat(), "llm_work": "garbage"},
        {"ts": "not-a-time", "llm_work": {"purposes": {"judge:anchor": purpose_row({})}}},
        {"no": "ts"},
    ]
    (row,) = lane_rows(history, ["judge:anchor"], now=NOW)
    assert row["class"] == "idle"


def test_build_status_includes_a_class_for_every_lane(tmp_path):
    state = tmp_path / "state"
    (state / "run_events").mkdir(parents=True)
    ts = datetime.now(UTC).isoformat()
    (state / "run_events" / "a.json").write_text(
        json.dumps(
            {
                "ts": ts,
                "lane": "chapter_agenda",
                "llm_work": {
                    "schema_version": 1,
                    "purposes": {"chapter-agenda": purpose_row({"blocked": 7})},
                },
            }
        )
    )
    status = build_status([], site_config={}, state_dir=state)
    lanes = {row["purpose"]: row for row in status["llm_lanes"]}
    assert set(lanes) == set(load_lanes())
    assert all(row["class"] for row in lanes.values())
    assert lanes["chapter-agenda"]["class"] == "a"
    assert lanes["judge:anchor"]["class"] == "unknown"


def test_status_html_renders_the_lane_table():
    status = build_status([], site_config={})
    html = to_status_html(status)
    assert 'id="llm_lanes"' in html and "function renderLlmLanes()" in html
    assert "renderLlmLanes();" in html
    assert "Slot productivity and" in html and "not measured" in html
    parsed = json.loads(
        re.search(r'<script id="status-data" type="application/json">(.*?)</script>', html, re.S)[1]
    )
    assert {row["purpose"] for row in parsed["llm_lanes"]} == set(load_lanes())
