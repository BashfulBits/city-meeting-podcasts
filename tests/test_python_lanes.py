from __future__ import annotations

from scripts import python_lanes
from scripts.python_lanes import (
    ASR_VERSION_FILE,
    DEFAULT_VERSION_FILE,
    check_asr_python_pins,
    lanes,
)


def test_workflow_python_pins_follow_version_files():
    _found, problems = lanes()
    assert problems == [], problems


def test_python_set_outside_setup_python_matches_asr_lane():
    problems = check_asr_python_pins()
    assert problems == [], problems


def test_lanes_cover_both_version_files_and_asr_constraints():
    found, _problems = lanes()
    pairs = {(lane["version_file"], lane["constraints"]) for lane in found}
    assert (ASR_VERSION_FILE, "constraints/asr.txt") in pairs
    assert (DEFAULT_VERSION_FILE, "constraints/prod.txt") in pairs
    assert all(lane["extras"] for lane in found)


def _write_workflow(tmp_path, monkeypatch, with_block: str, run: str) -> None:
    workflows = tmp_path / "workflows"
    workflows.mkdir()
    (workflows / "x.yml").write_text(
        "jobs:\n"
        "  j:\n"
        "    steps:\n"
        "      - uses: actions/checkout@" + "0" * 40 + "\n"
        "      - uses: actions/setup-python@" + "0" * 40 + "\n"
        f"        with:\n          {with_block}\n"
        f"      - run: {run}\n"
    )
    monkeypatch.setattr(python_lanes, "WORKFLOWS", workflows)


def test_literal_python_version_is_rejected(tmp_path, monkeypatch):
    _write_workflow(
        tmp_path, monkeypatch, 'python-version: "3.15"', "pip install -c constraints/prod.txt -e ."
    )
    _found, problems = lanes()
    assert any("literal python-version" in p for p in problems)


def test_asr_constraints_on_default_python_are_rejected(tmp_path, monkeypatch):
    _write_workflow(
        tmp_path,
        monkeypatch,
        f"python-version-file: {DEFAULT_VERSION_FILE}",
        "pip install -c constraints/asr.txt -e .",
    )
    _found, problems = lanes()
    assert any("constraints/asr.txt" in p for p in problems)
