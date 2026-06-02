"""Static contract tests for the GitHub Actions workflows.

These catch wiring mistakes that *no* build or unit test can surface, because the runtime fails
open. Case in point: graceful yield (``_newer_run_queued``) polls the Actions API and silently
returns False on any error — so a missing ``actions: read`` permission or an un-passed
``GITHUB_TOKEN`` disables it with zero signal (the build just runs to its wall-clock deadline).
Asserting the workflow wiring here fails the PR's ``test`` job the moment that regresses.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _build_step(workflow_file: str) -> tuple[dict, dict]:
    wf = yaml.safe_load((WORKFLOWS / workflow_file).read_text())
    job = next(iter(wf["jobs"].values()))
    step = next(s for s in job["steps"] if "citypods build" in str(s.get("run", "")))
    return wf, step


def test_deploy_workflow_wires_graceful_yield():
    """The production build's graceful yield needs the Actions API: ``actions: read`` AND
    ``GITHUB_TOKEN`` passed to the build step (Actions does not auto-export it). Missing either
    makes ``_newer_run_queued`` fail open — the build ignores a queued run and only stops at the
    wall-clock window — so assert the wiring statically (running the build can't catch it)."""
    wf, step = _build_step("deploy.yml")
    assert wf.get("permissions", {}).get("actions") == "read", (
        "deploy.yml must grant `actions: read`, or graceful yield silently no-ops"
    )
    assert "GITHUB_TOKEN" in (step.get("env") or {}), (
        "deploy.yml build step must pass GITHUB_TOKEN; Actions does not auto-export it"
    )
