"""Workflows that read canonical state must be able to reach its R2 manifest (#2280)."""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
STATE_READERS = ("scripts/audit_feeds.py", "scripts/remedy_p3_daily.py")
R2_ENV = ("R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")


def _missing_r2_env(workflow: dict) -> list[str]:
    """Names of state-reading steps whose effective env lacks an R2 setting."""
    problems = []
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if not any(reader in str(step.get("run", "")) for reader in STATE_READERS):
                continue
            env = {
                **(workflow.get("env") or {}),
                **(job.get("env") or {}),
                **(step.get("env") or {}),
            }
            absent = [name for name in R2_ENV if name not in env]
            if absent:
                problems.append(f"{job_name}/{step.get('name', '?')}: {absent}")
    return problems


def test_state_reading_workflows_configure_r2_coordination():
    """Without R2 the router reads a stale B2 shadow manifest and restores no source records."""
    problems = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        problems += [f"{path.name} {p}" for p in _missing_r2_env(yaml.safe_load(path.read_text()))]
    assert not problems, problems


def test_the_check_catches_a_missing_credential_and_config_in_another_step():
    workflow = {
        "jobs": {
            "audit": {
                "steps": [
                    {"name": "other", "env": {n: "x" for n in R2_ENV}, "run": "true"},
                    {
                        "name": "read",
                        "env": {"R2_BUCKET": "x"},
                        "run": "python scripts/audit_feeds.py",
                    },
                ]
            }
        }
    }
    assert _missing_r2_env(workflow) == ["audit/read: ['R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY']"]
