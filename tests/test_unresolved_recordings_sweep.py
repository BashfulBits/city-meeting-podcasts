"""Historical gaps and known false assignments cannot disappear from an offline sweep."""

import copy
import json
from pathlib import Path

import pytest

from scripts.sweep_unresolved_recordings import build_report, load_cases

ROOT = Path(__file__).resolve().parents[1]
SOURCE = "abbf5e25e078"
COUNCIL = "addison-tx-city-council"
PZ = "addison-tx-planning-and-zoning-commission"


def case(uid, guid, **changes):
    value = dict(
        case_id=f"case-{uid}",
        city="addison-tx",
        source_key=SOURCE,
        uid=uid,
        provider_guid=guid,
        question="Wrong assignment?",
        missing_evidence="Dated agenda",
        next_action="Find official packet",
        last_researched="2026-10-04",
        current_feed_assignments=[COUNCIL],
        status="open",
        disposition=None,
    )
    return {**value, **changes}


def setup_snapshot(tmp_path, records, cases):
    source = tmp_path / "state" / "sources" / SOURCE
    source.mkdir(parents=True)
    (source / "episodes.json").write_text(json.dumps({"episodes": records}))
    register = tmp_path / "cases.json"
    register.write_text(json.dumps({"schema_version": 1, "cases": cases}))
    return tmp_path / "state", register


def record(uid, guid, body):
    return dict(
        uid=uid, provider_guid=guid, body=body, title="Old recording", published="2007-06-01"
    )


def test_all_archive_gaps_and_matched_false_inclusions_remain_visible(tmp_path):
    records = {
        "old": record("old", "old-guid", "Unknown Historic Body"),
        "blank": record("blank", "blank-guid", None),
        "wrong": record("wrong", "wrong-guid", "Planning & Zoning Commission Work Session"),
        "excluded": record("excluded", "excluded-guid", "School Dedication"),
        "joint": record("joint", "295839", "Joint CPAC, P&Z, and City Council Meeting #1"),
    }
    cases = [
        case("wrong", "wrong-guid"),
        case("missing", "missing-guid"),
        case("joint", "295839"),
        case(
            "excluded",
            "excluded-guid",
            status="resolved",
            disposition="Approved ceremony exclusion",
        ),
    ]
    state, register = setup_snapshot(tmp_path, records, cases)
    paths = [register, state / "sources" / SOURCE / "episodes.json"]
    original_bytes = {p: p.read_bytes() for p in paths}
    original_records = copy.deepcopy(records)
    report = build_report(state, ROOT / "config", register)
    uncovered = {r["uid"]: r for r in report["uncovered_recordings"]}
    assert set(uncovered) == {"old", "blank", "excluded"}
    assert uncovered["excluded"]["disposition"] == "Approved ceremony exclusion"
    assert {r["uid"] for r in report["open_cases"]} == {"wrong", "joint"}
    wrong = next(r for r in report["open_cases"] if r["uid"] == "wrong")
    assert wrong["current_feed_assignments"] == [COUNCIL, PZ]
    joint = next(r for r in report["open_cases"] if r["uid"] == "joint")
    assert set(joint["current_feed_assignments"]) == {
        COUNCIL,
        PZ,
        "addison-tx-comprehensive-plan-advisory-committee",
    }
    assert {r["uid"] for r in report["assignment_changes"]} == {"wrong", "joint", "excluded"}
    assert [r["uid"] for r in report["missing_registered_records"]] == ["missing"]
    assert report == build_report(state, ROOT / "config", register)
    assert {p: p.read_bytes() for p in paths} == original_bytes
    assert records == original_records


@pytest.mark.parametrize(
    "change",
    [
        {"status": "resolved", "disposition": None},
        {"status": "ignored"},
        {"uid": ""},
        {"last_researched": "20261004"},
        {"current_feed_assignments": [4]},
        {"disposition": "Silent closure"},
    ],
)
def test_invalid_case_contract_rejected(tmp_path, change):
    _, register = setup_snapshot(tmp_path, {}, [{**case("old", "guid"), **change}])
    with pytest.raises(ValueError):
        load_cases(register)


def test_duplicate_identity_rejected(tmp_path):
    first = case("old", "guid")
    _, register = setup_snapshot(tmp_path, {}, [first, {**first, "case_id": "different"}])
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(register)


def test_record_binding_mismatch_is_visible_failure(tmp_path):
    state, register = setup_snapshot(
        tmp_path, {"old": record("old", "actual", "City Council")}, [case("old", "wrong")]
    )
    with pytest.raises(ValueError, match="city/GUID"):
        build_report(state, ROOT / "config", register)


def test_committed_register_validates_and_preserves_historical_obligations():
    cases = load_cases(ROOT / "review/evidence/unresolved-recording-cases.json")
    assert len([c for c in cases if "P&Z-only" in c["question"]]) == 47
    assert any(c["provider_guid"] == "56029" and c["status"] == "open" for c in cases)
    assert len([c for c in cases if c["question"].startswith("Historical Combined")]) == 30


def test_cli_refuses_overwriting_inputs(tmp_path, monkeypatch):
    from scripts.sweep_unresolved_recordings import main

    state, register = setup_snapshot(tmp_path, {}, [])
    monkeypatch.setattr(
        "sys.argv",
        [
            "sweep",
            "--state-root",
            str(state),
            "--config-root",
            str(ROOT / "config"),
            "--case-register",
            str(register),
            "--output",
            str(register),
        ],
    )
    original = register.read_bytes()
    with pytest.raises(SystemExit):
        main()
    assert register.read_bytes() == original
