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


def _on(wf: dict) -> dict:
    """Return a workflow's ``on`` block.

    PyYAML still treats the unquoted key ``on`` as a YAML 1.1 boolean, while GitHub Actions
    treats it as a string. Handle both so workflow tests can inspect triggers without forcing the
    workflow files to quote ``on`` just for the parser.
    """
    return wf.get("on") or wf.get(True) or {}


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


def test_deploy_enrich_treats_graceful_yield_as_success():
    """A superseded enrich run exits 143 after ``StopSignal`` fires. That is an expected yield,
    not a failed Pages deployment, because deploy already happened before enrich."""
    _wf, job = _job("deploy.yml")
    enrich = next(s for s in job["steps"] if "citypods enrich" in str(s.get("run", "")))
    run = str(enrich.get("run", ""))
    assert 'if [ "$code" -eq 143 ] &&' in run
    assert 'grep -q "stop: newer build queued behind this run"' in run
    assert "Enrich yielded to newer run" in run


def test_deploy_skips_docs_only_pushes_but_not_deploy_inputs():
    """Docs/review-only merges to main should not start the expensive Build & Deploy workflow.

    Keep the ignore list narrow: generated Pages output and build inputs must still trigger a
    deploy when they change.
    """
    wf, _ = _job("deploy.yml")
    push = _on(wf)["push"]
    ignored = set(push.get("paths-ignore", []))

    assert push.get("branches") == ["main"]
    assert {
        "review/**",
        "AGENTS.md",
        "ARCHITECTURE.md",
        "CHANGELOG.md",
        "CLAUDE.md",
        "README.md",
        "ROADMAP.md",
        "PLAN.md",
        "CONTRIBUTING.md",
        "MIGRATION.md",
        "SECURITY.md",
        "VISION.md",
        ".github/ADD_CITY.md",
        ".github/ISSUE_TEMPLATE/**",
        ".github/PULL_REQUEST_TEMPLATE.md",
    } <= ignored

    assert (
        not {
            "docs/**",
            "config/**",
            "templates/**",
            "citypods/**",
            "scripts/**",
            ".github/workflows/**",
        }
        & ignored
    )
