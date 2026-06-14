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


def test_enrich_workflow_wires_graceful_yield():
    """Graceful yield needs the Actions API: ``actions: read`` AND ``GITHUB_TOKEN`` available to the
    time-bounded **enrich** phase (Actions does not auto-export it). Missing either makes
    ``_newer_run_queued`` fail open — enrich ignores a queued run and only stops at the wall-clock
    window — so assert the wiring statically (running it can't catch it). Lives in enrich.yml since
    H11b split the heavy phase out of the render-only deploy."""
    wf, job = _job("enrich.yml")
    assert wf.get("permissions", {}).get("actions") == "read", (
        "enrich.yml must grant `actions: read`, or graceful yield silently no-ops"
    )
    enrich = next(s for s in job["steps"] if "citypods enrich" in str(s.get("run", "")))
    # The token may be on the job (shared by all steps) or the enrich step itself.
    token_sources = {**(job.get("env") or {}), **(enrich.get("env") or {})}
    assert "GITHUB_TOKEN" in token_sources, (
        "enrich.yml must pass GITHUB_TOKEN to the enrich phase; Actions does not auto-export it"
    )


def test_deploy_is_render_only():
    """H11b: deploy.yml is a render-only job — it publishes feeds/pages and never runs the heavy
    phase, so encoding/transcription can't block or redden the Pages deploy. Guard that the enrich
    step (and the ffmpeg/Whisper machinery + graceful-yield token it needed) is GONE, and that
    render still precedes deploy."""
    wf, job = _job("deploy.yml")
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    uses = " ".join(str(s.get("uses", "")) for s in job["steps"])
    assert "citypods enrich" not in runs, "deploy.yml must not run the heavy enrich phase (H11b)"
    assert "apt-get install -y ffmpeg" not in runs, "render-only deploy needs no ffmpeg"
    assert "prepare_whisper" not in runs, "render-only deploy needs no Whisper model"
    # The graceful-yield token is only for the time-bounded enrich phase, which moved out.
    assert "actions" not in (wf.get("permissions") or {}), (
        "deploy.yml must drop `actions: read` once enrich (the only Actions-API caller) moves out"
    )
    render = _step_index(job, "citypods build --phase render")
    deploy = _step_index(job, "actions/deploy-pages")
    assert render >= 0 and deploy >= 0, "render and deploy steps required"
    assert render < deploy, "deploy.yml must render before deploying"
    # The Pages plumbing stays on deploy, not enrich.
    assert "actions/upload-pages-artifact" in uses and "actions/deploy-pages" in uses


def test_enrich_workflow_is_isolated_from_pages():
    """The heavy phase runs in its own workflow with a concurrency group distinct from `pages`, so
    an in-flight/queued enrich never holds up or reddens a deploy. It must not deploy to Pages."""
    wf, job = _job("enrich.yml")
    triggers = _on(wf)
    assert set(triggers) >= {"schedule", "workflow_dispatch"}
    assert "push" not in triggers, "enrich is scheduled/manual, not run on every push to main"
    assert wf.get("concurrency", {}).get("group") == "enrich"
    assert wf.get("concurrency", {}).get("group") != "pages"
    assert wf.get("concurrency", {}).get("cancel-in-progress") is False
    uses = " ".join(str(s.get("uses", "")) for s in job["steps"])
    assert "deploy-pages" not in uses and "upload-pages-artifact" not in uses
    assert _step_index(job, "citypods enrich") >= 0


def test_enrich_treats_graceful_yield_as_success():
    """A superseded enrich run exits 143 after ``StopSignal`` fires. That is an expected yield, not
    a failure — and (post-H11b) it never touched the Pages deploy, which is a separate workflow."""
    _wf, job = _job("enrich.yml")
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


def test_asr_bench_workflow_is_manual_serial_and_publishes_report():
    """H6a is a manual benchmark harness, not another scheduled production worker."""
    wf, job = _job("asr-bench.yml")
    triggers = _on(wf)
    assert set(triggers) == {"workflow_dispatch"}

    assert wf.get("permissions", {}).get("contents") == "read"
    assert wf.get("concurrency", {}).get("group") == "asr-bench"
    assert wf.get("concurrency", {}).get("cancel-in-progress") is False
    assert job.get("timeout-minutes") == 350

    inputs = _on(wf)["workflow_dispatch"]["inputs"]
    assert inputs["models"]["default"] == "large-v3-turbo,small.en,base.en"
    assert inputs["beam_sizes"]["default"] == "5,3,1"
    assert inputs["cpu_threads"]["default"] == "4,2,1"

    install = next(s for s in job["steps"] if s.get("name") == "Install")
    assert 'pip install -e ".[asr]"' in install["run"]

    bench = next(s for s in job["steps"] if s.get("name") == "Run ASR benchmark")
    run = bench["run"]
    assert "python -m citypods.cli asr-bench" in run
    assert '--beam-size "$beam"' in run
    assert 'timeout "${PROFILE_TIMEOUT_MINUTES}m"' in run
    assert "exactly three comma-separated values for max,med,min" in run
    assert 'cat "$log" >> "$GITHUB_STEP_SUMMARY"' in run

    assert _step_index(job, "actions/upload-artifact") > _step_index(
        job, "python -m citypods.cli asr-bench"
    )
