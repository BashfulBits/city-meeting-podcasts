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


def _job(workflow_file: str) -> dict:
    wf = yaml.safe_load((WORKFLOWS / workflow_file).read_text())
    return wf, next(iter(wf["jobs"].values()))


def _step_index(job: dict, needle: str) -> int:
    """Index of the first step whose `run` or `uses` contains `needle` (or -1)."""
    for i, s in enumerate(job["steps"]):
        if needle in str(s.get("run", "")) or needle in str(s.get("uses", "")):
            return i
    return -1


def test_deploy_workflow_wires_graceful_yield():
    """Graceful yield needs the Actions API: ``actions: read`` AND ``GITHUB_TOKEN`` available to the
    time-bounded **enrich** phase (Actions does not auto-export it). Missing either makes
    ``_newer_run_queued`` fail open — enrich ignores a queued run and only stops at the wall-clock
    window — so assert the wiring statically (running it can't catch it)."""
    wf, job = _job("deploy.yml")
    assert wf.get("permissions", {}).get("actions") == "read", (
        "deploy.yml must grant `actions: read`, or graceful yield silently no-ops"
    )
    enrich = next(s for s in job["steps"] if "citypods enrich" in str(s.get("run", "")))
    # The token may be on the job (shared by both phases) or the enrich step itself.
    token_sources = {**(job.get("env") or {}), **(enrich.get("env") or {})}
    assert "GITHUB_TOKEN" in token_sources, (
        "deploy.yml must pass GITHUB_TOKEN to the enrich phase; Actions does not auto-export it"
    )


def test_deploy_renders_and_deploys_before_enriching():
    """The whole point of the split: publish the fast render BEFORE the heavy enrich runs. Guard
    the order — render → deploy → enrich — so a reorder that reintroduces "deploy waits for the
    audio window" (issue #63) fails the PR's test job."""
    _wf, job = _job("deploy.yml")
    render = _step_index(job, "citypods build --phase render")
    deploy = _step_index(job, "actions/deploy-pages")
    enrich = _step_index(job, "citypods enrich")
    assert render >= 0 and deploy >= 0 and enrich >= 0, "render, deploy, and enrich steps required"
    assert render < deploy < enrich, (
        "deploy.yml must render → deploy → enrich (deploy the fast outputs before the heavy phase)"
    )
