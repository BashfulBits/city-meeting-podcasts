from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from citypods.audit_remedy import BodyDecision, P3Vote
from citypods.audit_remedy_report import (
    P3ReportInput,
    build_p3_report,
    load_report_input,
    write_report_exclusive,
)

EVIDENCE_HASH = "e" * 64
POLICY_HASH = "f" * 64


def vote(route_id: str, *, feed: str = "test-city-council", digest: str = "a") -> P3Vote:
    return P3Vote(
        route_id=route_id,
        upstream_model=f"model/{route_id}",
        reasoning_level="high",
        evidence_hash=EVIDENCE_HASH,
        policy_hash=POLICY_HASH,
        response_hash=digest * 64,
        decision=BodyDecision(
            finding_id="finding-1",
            action="union",
            target_feeds=[feed],
            rationale="Recurring public meeting body.",
        ),
    )


def record(**updates) -> P3ReportInput:
    values = {
        "schema_version": 1,
        "finding_id": "finding-1",
        "evidence_hash": EVIDENCE_HASH,
        "policy_hash": POLICY_HASH,
        "evidence_summary": "Observed three recurring sessions.",
        "feeds": [
            {"slug": "test-city-council", "title": "Test City Council"},
            {"slug": "test-city-library-board", "title": "Test City Library Board"},
        ],
        "initial_votes": [
            vote("beatapi_gpt_6_1_sol_free"),
            vote("orcarouter_zai_glm_5_3_flash_free"),
        ],
        "initial_validators": {"openai/gpt-6.1-sol": True, "zai/glm-5.3-flash": True},
    }
    values.update(updates)
    return P3ReportInput.model_validate(values).validate_contract()


def test_report_cli_context_fails_closed_without_trusted_admissions():
    report = build_p3_report(record())

    assert report["decision"]["disposition"] == "policy_hold"
    assert "proposal_candidate" not in report
    assert report["input_hash"]


def test_trusted_runtime_admission_replays_consensus_without_issue_write():
    report = build_p3_report(
        record(),
        admitted_models={"openai/gpt-6.1-sol", "zai/glm-5.3-flash"},
    )

    assert report["decision"]["disposition"] == "pr_candidate"
    assert report["proposal_candidate"]["route_id"] == "beatapi_gpt_6_1_sol_free"
    assert report["proposal_candidate"]["response_hash"] == "a" * 64


def test_report_renders_manual_issue_only_for_failed_consensus():
    votes = [
        vote("beatapi_gpt_6_1_sol_free"),
        vote("orcarouter_zai_glm_5_3_flash_free", feed="test-city-library-board", digest="b"),
    ]
    item = record(initial_votes=votes)
    models = {
        "openai/gpt-6.1-sol",
        "zai/glm-5.3-flash",
        "deepseek/deepseek-v4.1-flash",
        "typesafe/jev-1.13",
    }
    held = build_p3_report(item, admitted_models=models)
    assert held["decision"]["disposition"] == "policy_hold"
    assert "manual_issue_candidate" not in held

    adjudication = {
        "deepseek": vote(
            "beatapi_deepseek_v4_1_flash_free", feed="test-city-library-board", digest="c"
        ).model_dump(mode="json"),
        "jev_route_id": "beatapi_jev_1_13_free",
        "jev_upstream_model": "model/jev",
        "evidence_hash": EVIDENCE_HASH,
        "policy_hash": POLICY_HASH,
        "response_hash": "d" * 64,
        "choice": "C",
        "supported_a": 0.2,
        "supported_b": 0.3,
    }
    item = record(initial_votes=votes, adjudication=adjudication)
    report = build_p3_report(
        item,
        admitted_models=models,
    )
    assert report["decision"]["disposition"] == "issue_candidate"
    assert "Include `test-city-council`" in report["manual_issue_candidate"]
    assert "Exclude `test-city-library-board`" in report["manual_issue_candidate"]
    assert "Possible / hold" not in report["manual_issue_candidate"]


def test_input_rejects_unknown_admission_claim_and_duplicate_feed():
    data = record().model_dump(mode="json")
    with pytest.raises(ValueError, match="admitted_routes"):
        P3ReportInput.model_validate({**data, "admitted_routes": ["fake"]})
    with pytest.raises(ValueError, match="unique"):
        record(feeds=[{"slug": "test-city-council", "title": "A"}] * 2)
    with pytest.raises(ValueError, match="exactly the two initial models"):
        record(initial_validators={"untrusted/model": True})
    with pytest.raises(ValueError, match="exactly the configured"):
        record(initial_votes=record().initial_votes + [vote("beatapi_gpt_6_1_sol_free")])
    with pytest.raises(ValueError, match="valid slugs"):
        record(feeds=[{"slug": "../outside", "title": "A"}])


def test_input_rejects_initial_vote_binding_mismatch():
    votes = list(record().initial_votes)
    votes[0] = votes[0].model_copy(update={"evidence_hash": "1" * 64})

    with pytest.raises(ValueError, match="initial vote evidence or policy binding"):
        record(initial_votes=votes)


def test_input_rejects_adjudication_binding_mismatch():
    adjudication = {
        "deepseek": vote("beatapi_deepseek_v4_1_flash_free").model_dump(mode="json"),
        "jev_route_id": "beatapi_jev_1_13_free",
        "jev_upstream_model": "model/jev",
        "evidence_hash": "1" * 64,
        "policy_hash": POLICY_HASH,
        "response_hash": "d" * 64,
        "choice": "A",
    }

    with pytest.raises(ValueError, match="adjudication evidence or policy binding"):
        record(adjudication=adjudication)


def test_input_rejects_nested_deepseek_vote_binding_mismatch():
    deepseek = vote("beatapi_deepseek_v4_1_flash_free").model_copy(update={"policy_hash": "1" * 64})
    adjudication = {
        "deepseek": deepseek.model_dump(mode="json"),
        "jev_route_id": "beatapi_jev_1_13_free",
        "jev_upstream_model": "model/jev",
        "evidence_hash": EVIDENCE_HASH,
        "policy_hash": POLICY_HASH,
        "response_hash": "d" * 64,
        "choice": "A",
    }

    with pytest.raises(ValueError, match="DeepSeek vote evidence or policy binding"):
        record(adjudication=adjudication)


def test_json_loader_rejects_duplicate_keys(tmp_path: Path):
    input_path = tmp_path / "input.json"
    input_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON key"):
        load_report_input(input_path)


def test_writer_is_exclusive_and_never_overwrites_input(tmp_path: Path):
    source = tmp_path / "source.json"
    output = tmp_path / "report.json"
    source.write_text("{}", encoding="utf-8")
    report = {"schema_version": 1}

    with pytest.raises(ValueError, match="differ"):
        write_report_exclusive(source, report, input_path=source)
    write_report_exclusive(output, report, input_path=source)
    with pytest.raises(FileExistsError):
        write_report_exclusive(output, report, input_path=source)
    assert json.loads(output.read_text(encoding="utf-8")) == report


def test_writer_rejects_dangling_output_symlink(tmp_path: Path):
    source = tmp_path / "source.json"
    output = tmp_path / "report.json"
    target = tmp_path / "missing-target.json"
    source.write_text("{}", encoding="utf-8")
    output.symlink_to(target)

    with pytest.raises(FileExistsError):
        write_report_exclusive(output, {"schema_version": 1}, input_path=source)
    assert not target.exists()


def test_cli_is_local_report_only_and_creates_a_held_report(tmp_path: Path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "input.json"
    output = tmp_path / "report.json"
    guard = tmp_path / "offline_guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(
        "import socket\n"
        "import subprocess\n"
        "def blocked(*args, **kwargs):\n"
        "    raise RuntimeError('offline CLI attempted network/process access')\n"
        "socket.create_connection = blocked\n"
        "socket.socket.connect = blocked\n"
        "socket.socket.connect_ex = blocked\n"
        "subprocess.Popen = blocked\n",
        encoding="utf-8",
    )
    source.write_text(record().model_dump_json(), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(root / "scripts/report_p3_remedy.py"),
            "--input",
            str(source),
            "--out",
            str(output),
        ],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env={**os.environ, "PYTHONPATH": os.pathsep.join((str(guard), str(root)))},
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text(encoding="utf-8"))["decision"]["disposition"] == (
        "policy_hold"
    )


def test_legacy_input_hash_matches_pre_role_plan_contract():
    import hashlib

    saved = record()
    old_payload = saved.model_dump(mode="json", exclude={"role_plan", "initial_failures"})
    encoded = json.dumps(old_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert build_p3_report(saved)["input_hash"] == hashlib.sha256(encoded.encode()).hexdigest()
