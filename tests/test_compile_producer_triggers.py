"""review/58 WP6b: the Worker's producer trigger table compiles from config; no JSON drift."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "compile_producer_triggers", REPO_ROOT / "scripts" / "compile_producer_triggers.py"
)
compile_producer_triggers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compile_producer_triggers)


def test_committed_json_matches_the_yaml():
    table = compile_producer_triggers.load_triggers()
    assert compile_producer_triggers.OUTPUT_JSON.read_text(
        encoding="utf-8"
    ) == compile_producer_triggers.render(table)
    assert compile_producer_triggers.main(["--check"]) == 0


def test_check_fails_when_the_json_is_stale(tmp_path, monkeypatch, capsys):
    stale = tmp_path / "producer_triggers.json"
    stale.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(compile_producer_triggers, "OUTPUT_JSON", stale)
    assert compile_producer_triggers.main(["--check"]) == 1
    assert "out of date" in capsys.readouterr().err


def test_every_n_hours_expands_to_explicit_utc_hours():
    table = json.loads(compile_producer_triggers.render(compile_producer_triggers.load_triggers()))
    by_type = {entry["event_type"]: entry for entry in table["triggers"]}
    assert by_type["llm-deferred-sweep"]["hours"] == list(range(24))
    assert by_type["chapter-agenda"]["hours"] == list(range(0, 24, 2))
    assert by_type["chapter-locator"]["hours"] == list(range(1, 24, 2))
    assert by_type["tag"]["hours"] == list(range(0, 24, 2))
    assert by_type["moments"]["hours"] == list(range(1, 24, 2))
    assert by_type["judge"]["hours"] == list(range(0, 24, 2))
    assert table["max_triggers_per_tick"] <= compile_producer_triggers.MAX_TRIGGERS_PER_TICK_CEILING
    # The compiled table carries what the Worker needs and nothing the workflows own.
    assert all(
        set(entry) == {"event_type", "workflow", "minute", "hours"} for entry in table["triggers"]
    )


_HEAD = "max_triggers_per_tick: 3\ntimeout_seconds: 5\ntriggers:\n"


def _entry(**overrides) -> str:
    fields = {
        "event_type": "sweep",
        "workflow": "sweep.yml",
        "minute": 52,
        "every_n_hours": 1,
        "schedule_guard_minutes": 45,
        **overrides,
    }
    lines = [
        f"  - {k}: {v}" if i == 0 else f"    {k}: {v}" for i, (k, v) in enumerate(fields.items())
    ]
    return "\n".join(lines) + "\n"


@pytest.mark.parametrize(
    "body, message",
    [
        (_HEAD + _entry(minute=50), "cleanup minute"),
        (_HEAD + _entry(minute=60), r"minute must be an integer in \[0, 59\]"),
        (_HEAD + _entry(every_n_hours=5), "must divide 24"),
        (_HEAD + _entry(every_n_hours=2, hour_offset=2), "hour_offset"),
        (_HEAD + _entry(hours="[1, 2]"), "exactly one of"),
        (_HEAD + _entry(event_type="Bad Name"), "plain slug"),
        (_HEAD + _entry(workflow="../x.yml"), r"\.yml file name"),
        (_HEAD + _entry(schedule_guard_minutes=60), "schedule_guard_minutes"),
        (_HEAD + _entry() + _entry(), "repeats event_type"),
        (_HEAD + _entry(extra=1), "unknown keys"),
        ("max_triggers_per_tick: 4\ntimeout_seconds: 5\ntriggers:\n" + _entry(), "max_triggers"),
        ("max_triggers_per_tick: 3\ntimeout_seconds: 30\ntriggers:\n" + _entry(), "timeout"),
        (
            "max_triggers_per_tick: 1\ntimeout_seconds: 5\ntriggers:\n"
            + _entry()
            + _entry(event_type="other", workflow="other.yml", every_n_hours=2),
            "above max_triggers_per_tick",
        ),
    ],
)
def test_rejects_bad_tables(tmp_path, body, message):
    path = tmp_path / "t.yml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(SystemExit, match=message):
        compile_producer_triggers.load_triggers(path, cleanup_interval=10)


def test_explicit_hours_list_is_sorted(tmp_path):
    path = tmp_path / "t.yml"
    body = _HEAD + _entry(schedule_guard_minutes=30).replace("every_n_hours: 1", "hours: [13, 1]")
    path.write_text(body, encoding="utf-8")
    table = compile_producer_triggers.load_triggers(path, cleanup_interval=10)
    assert table["triggers"][0]["hours"] == [1, 13]
