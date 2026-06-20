"""Static contract tests for the GitHub Actions workflows.

These catch wiring mistakes that *no* build or unit test can surface, because the runtime fails
open. Case in point: graceful yield (``_newer_run_queued``) polls the Actions API and silently
returns False on any error — so a missing ``actions: read`` permission or an un-passed
``GITHUB_TOKEN`` disables it with zero signal (the build just runs to its wall-clock deadline).
Asserting the workflow wiring here fails the PR's ``test`` job the moment that regresses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _job(workflow_file: str, job_name: str | None = None) -> dict:
    wf = yaml.safe_load((WORKFLOWS / workflow_file).read_text())
    job = wf["jobs"][job_name] if job_name else next(iter(wf["jobs"].values()))
    return wf, job


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


# H6b split the combined enrich into two sharded, lane-pinned workflows.
# Third element is the job name within the workflow file (audio.yml has a wait-for-contracts
# pre-job so the heavy job must be addressed by name, not by position).
HEAVY_WORKFLOWS = [("audio.yml", "audio", "audio"), ("asr.yml", "transcribe", "asr")]


def test_workflows_use_node24_cache_actions_without_force_flag():
    """actions/cache v5 runs on Node 24; the old force flag should not linger."""
    workflow_text = "\n".join(path.read_text() for path in WORKFLOWS.glob("*.yml"))
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24" not in workflow_text
    assert "actions/cache@v4" not in workflow_text
    assert "actions/cache/restore@v4" not in workflow_text
    assert "actions/cache@v5" in workflow_text


@pytest.mark.parametrize("workflow,lane,job_name", HEAVY_WORKFLOWS)
def test_heavy_workflow_wires_graceful_yield(workflow, lane, job_name):
    """Graceful yield needs the Actions API: ``actions: read`` AND ``GITHUB_TOKEN`` for the
    time-bounded heavy phase (Actions does not auto-export it). Missing either makes
    ``_newer_run_queued`` fail open — the shard ignores a queued run and only stops at the
    wall-clock window — so assert the wiring statically (running it can't catch it)."""
    wf, job = _job(workflow, job_name)
    assert wf.get("permissions", {}).get("actions") == "read", (
        f"{workflow} must grant `actions: read`, or graceful yield silently no-ops"
    )
    step = next(s for s in job["steps"] if "citypods enrich" in str(s.get("run", "")))
    token_sources = {**(job.get("env") or {}), **(step.get("env") or {})}
    assert "GITHUB_TOKEN" in token_sources, (
        f"{workflow} must pass GITHUB_TOKEN to the heavy phase; Actions does not auto-export it"
    )


@pytest.mark.parametrize("workflow,lane,job_name", HEAVY_WORKFLOWS)
def test_heavy_workflow_is_sharded_and_lane_pinned(workflow, lane, job_name):
    """Each heavy workflow runs a source-sharded matrix pinned to one lane, so concurrent shards own
    disjoint record files (scoped push_state) and the two ASR models never co-load."""
    wf, job = _job(workflow, job_name)
    shards = job.get("strategy", {}).get("matrix", {}).get("shard")
    assert shards == [0, 1, 2, 3], f"{workflow} must shard by source_key (matrix.shard)"
    step = next(s for s in job["steps"] if "citypods enrich" in str(s.get("run", "")))
    run = str(step["run"])
    n = len(shards)
    assert f"--lane {lane}" in run, f"{workflow} must pin --lane {lane}"
    assert f"--shard ${{{{ matrix.shard }}}}/{n}" in run, (
        f"{workflow} must pass --shard <matrix.shard>/{n} matching the matrix size"
    )


@pytest.mark.parametrize("workflow,group", [("audio.yml", "audio"), ("asr.yml", "asr")])
def test_heavy_workflow_is_isolated_from_pages(workflow, group):
    """Each heavy workflow has its own concurrency group (distinct from `pages` and each other), is
    scheduled/manual (not on every push), and never deploys to Pages — so a deploy is never canceled
    by audio/ASR work."""
    wf, job = _job(workflow, job_name=group)
    triggers = _on(wf)
    assert set(triggers) >= {"schedule", "workflow_dispatch"}
    assert "push" not in triggers, f"{workflow} is scheduled/manual, not run on every push"
    assert wf.get("concurrency", {}).get("group") == group
    assert wf.get("concurrency", {}).get("group") != "pages"
    assert wf.get("concurrency", {}).get("cancel-in-progress") is False
    uses = " ".join(str(s.get("uses", "")) for s in job["steps"])
    assert "deploy-pages" not in uses and "upload-pages-artifact" not in uses


@pytest.mark.parametrize("workflow,_lane,job_name", HEAVY_WORKFLOWS)
def test_heavy_workflow_treats_graceful_yield_as_success(workflow, _lane, job_name):
    """The designed graceful yield is exit 0 (the shard stops starting new work after ``StopSignal``
    fires, finishes in-flight work, then exits). A 143 (SIGTERM) after the stop signal is the
    Actions hard cap landing mid-yield — still an expected yield, not a failure — and it never
    touched the Pages deploy, which is a separate workflow."""
    _wf, job = _job(workflow, job_name)
    step = next(s for s in job["steps"] if "citypods enrich" in str(s.get("run", "")))
    run = str(step.get("run", ""))
    assert 'if [ "$code" -eq 143 ] &&' in run
    assert 'grep -q "stop: newer build queued behind this run"' in run
    assert "yielded to newer run" in run


def test_no_combined_enrich_workflow():
    """H6b removed the combined enrich.yml — audio.yml + asr.yml replace it. A lingering enrich.yml
    would re-add a third full record writer and reopen the cross-writer clobber."""
    assert not (WORKFLOWS / "enrich.yml").exists()


def test_asr_workflow_runs_every_five_hours():
    wf, _ = _job("asr.yml")
    schedules = _on(wf).get("schedule", [])
    assert {item.get("cron") for item in schedules} == {"0 */5 * * *"}


def test_asr_uses_one_canonical_snapshot_and_shard_plan():
    _wf, reconcile = _job("asr.yml", job_name="reconcile")
    _wf, asr = _job("asr.yml", job_name="asr")

    reconcile_runs = "\n".join(str(step.get("run", "")) for step in reconcile["steps"])
    assert "citypods compute reconcile" in reconcile_runs
    assert "citypods compute plan-shards" in reconcile_runs
    assert "--lane transcribe" in reconcile_runs
    assert "--shards 4" in reconcile_runs
    upload = next(
        step for step in reconcile["steps"] if "actions/upload-artifact" in step.get("uses", "")
    )
    assert upload["with"]["name"] == "asr-planner-${{ github.run_id }}"
    assert ".citypods-state" in upload["with"]["path"]
    assert ".citypods-asr-shard-plan.json" in upload["with"]["path"]
    assert upload["with"]["include-hidden-files"] is True

    download = next(
        step for step in asr["steps"] if "actions/download-artifact" in step.get("uses", "")
    )
    assert download["with"]["name"] == "asr-planner-${{ github.run_id }}"
    assert not any(step.get("name") == "Restore build state" for step in asr["steps"])
    assert not any(
        ".citypods-state" in str(step.get("with", {}).get("path", ""))
        and "actions/cache" in step.get("uses", "")
        for step in asr["steps"]
    )
    enrich = next(step for step in asr["steps"] if "citypods enrich" in str(step.get("run", "")))
    assert "--shard-plan .citypods-asr-shard-plan.json" in enrich["run"]
    assert "--state-snapshot-restored" in enrich["run"]


def test_audio_lane_needs_no_whisper():
    """The audio lane never runs ASR, so it must not install the asr extra or download Whisper —
    that's wasted runner time and memory."""
    _wf, job = _job("audio.yml", job_name="audio")
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "prepare_whisper" not in runs
    assert '".[asr' not in runs and "[asr,storage]" not in runs


def test_audio_uses_pinned_runner_image_with_verified_host_fallback():
    wf, job = _job("audio.yml", job_name="audio")
    env = job["env"]
    image = env["AUDIO_RUNNER_IMAGE"]
    assert image.startswith("ghcr.io/bashfulbits/citypods-audio-runner:")
    assert not image.endswith(":latest")
    assert env["FFMPEG_URL"].startswith(
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/autobuild-"
    )
    assert len(env["FFMPEG_SHA256"]) == 64
    assert wf["permissions"]["packages"] == "read"

    select = next(s for s in job["steps"] if s.get("name") == "Select audio runtime")
    assert 'timeout 300 docker pull "${AUDIO_RUNNER_IMAGE}"' in select["run"]
    assert "import boto3, citypods" in select["run"]
    assert "timeout 300 python scripts/install_static_ffmpeg.py" in select["run"]
    assert '--sha256 "${FFMPEG_SHA256}"' in select["run"]
    assert '"${FFMPEG_FALLBACK_DIR}/bin/ffprobe" -version' in select["run"]

    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "apt-get" not in runs
    assert "docker run --rm --init" in runs
    assert "python -m citypods.cli enrich --lane audio" in runs
    assert job["env"]["GRANICUS_PROXY_BASE_URL"] == "${{ secrets.GRANICUS_PROXY_BASE_URL }}"
    assert job["env"]["GRANICUS_PROXY_TOKEN"] == "${{ secrets.GRANICUS_PROXY_TOKEN }}"
    audio_step = next(s for s in job["steps"] if "citypods enrich" in str(s.get("run", "")))
    assert "--env GRANICUS_PROXY_BASE_URL" in audio_step["run"]
    assert "--env GRANICUS_PROXY_TOKEN" in audio_step["run"]


def test_audio_runner_image_build_is_scheduled_and_publishes_ghcr():
    wf, job = _job("audio-runner-image.yml", job_name="build")
    assert {"schedule", "workflow_dispatch", "push"} <= set(_on(wf))
    assert wf["permissions"]["packages"] == "write"
    assert job["timeout-minutes"] == 30
    assert wf["env"]["IMAGE"].endswith(":py312-ffmpeg71-v1")
    assert not wf["env"]["IMAGE"].endswith(":latest")

    build = next(s for s in job["steps"] if "docker/build-push-action" in s.get("uses", ""))
    assert build["with"]["push"] is True
    assert build["with"]["platforms"] == "linux/amd64"
    assert build["with"]["file"] == ".github/audio-runner/Dockerfile"
    assert "FFMPEG_SHA256=" in build["with"]["build-args"]
    assert _step_index(job, "ffprobe -version") > _step_index(job, "docker/build-push-action")

    _audio_wf, audio_job = _job("audio.yml", job_name="audio")
    assert audio_job["env"]["FFMPEG_URL"] == wf["env"]["FFMPEG_URL"]
    assert audio_job["env"]["FFMPEG_SHA256"] == wf["env"]["FFMPEG_SHA256"]

    dockerfile = (
        Path(__file__).resolve().parents[1] / ".github" / "audio-runner" / "Dockerfile"
    ).read_text()
    assert "python:3.12-slim-bookworm@sha256:" in dockerfile
    assert "apt-get" not in dockerfile


def test_asr_uses_verified_static_ffmpeg_without_baking_whisper_weights():
    _wf, job = _job("asr.yml", job_name="asr")
    env = job["env"]
    image_wf, _image_job = _job("audio-runner-image.yml", job_name="build")

    assert env["FFMPEG_URL"] == image_wf["env"]["FFMPEG_URL"]
    assert env["FFMPEG_SHA256"] == image_wf["env"]["FFMPEG_SHA256"]
    assert len(env["FFMPEG_SHA256"]) == 64

    install = next(s for s in job["steps"] if s.get("name") == "Install verified static ffmpeg")
    run = install["run"]
    assert "timeout 300 python scripts/install_static_ffmpeg.py" in run
    assert '--sha256 "${FFMPEG_SHA256}"' in run
    assert '"${FFMPEG_DIR}/bin/ffmpeg" -version' in run
    assert '"${FFMPEG_DIR}/bin" >> "$GITHUB_PATH"' in run

    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "apt-get" not in runs
    assert "prepare_whisper.py" in runs
    assert "docker run" not in runs
    assert "faster-whisper-large-v3-turbo" in str(job["steps"])
    install = next(s for s in job["steps"] if s.get("name") == "Install")
    assert 'pip install -e ".[asr-transcribe,storage]"' in install["run"]
    assert "asr-align" not in install["run"]


def test_granicus_sustained_probe_is_manual_isolated_and_archived():
    wf, job = _job("granicus-probe.yml", job_name="probe")

    assert set(_on(wf)) == {"workflow_dispatch"}
    inputs = _on(wf)["workflow_dispatch"]["inputs"]
    assert inputs["probe_kind"]["default"] == "transport"
    assert inputs["probe_kind"]["options"] == ["transport", "worker", "sustained"]
    assert wf["permissions"] == {"contents": "read", "actions": "read"}
    assert wf["concurrency"]["group"] == "audio"
    assert wf["concurrency"]["cancel-in-progress"] is False

    runs = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "probe_granicus_sustained.py" in runs
    assert "probe_granicus_transport.py" in runs
    assert "probe_granicus_worker.py" in runs
    assert "--range-mib" in runs
    assert "--full-download-max-mib" in runs
    assert "--full-download-count" in runs
    assert "$((" not in runs
    assert "audio.yml --status in_progress" in runs
    assert "audio.yml --status queued" in runs
    assert "--sha256" in runs

    upload = next(step for step in job["steps"] if "upload-artifact" in step.get("uses", ""))
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "granicus-*-results.json"


def test_ci_runs_granicus_worker_unit_tests():
    _wf, job = _job("ci.yml", job_name="test")
    step = next(
        step for step in job["steps"] if step.get("name") == "Test Granicus Cloudflare Worker"
    )
    assert step["working-directory"] == "workers/granicus-media-proxy"
    assert step["run"] == "npm test"


def test_granicus_worker_deploy_is_path_scoped_and_uses_cloudflare_secrets():
    wf, job = _job("granicus-worker-deploy.yml", job_name="deploy")
    triggers = _on(wf)
    paths = triggers["push"]["paths"]
    assert triggers["push"]["branches"] == ["main"]
    assert "workers/granicus-media-proxy/src/**" in paths
    assert "workers/granicus-media-proxy/wrangler.jsonc" in paths
    assert "workers/granicus-media-proxy/README.md" not in paths
    assert "workflow_dispatch" in triggers
    assert wf["permissions"] == {"contents": "read"}

    test_step = next(step for step in job["steps"] if step.get("name") == "Test Worker")
    assert test_step["working-directory"] == "workers/granicus-media-proxy"
    deploy = next(step for step in job["steps"] if step.get("name") == "Deploy Worker")
    assert deploy["uses"] == "cloudflare/wrangler-action@v3"
    assert deploy["with"]["workingDirectory"] == "workers/granicus-media-proxy"
    assert deploy["with"]["apiToken"] == "${{ secrets.CLOUDFLARE_API_TOKEN }}"
    assert deploy["with"]["accountId"] == "${{ secrets.CLOUDFLARE_ACCOUNT_ID }}"
    assert "secrets" not in deploy["with"], "PROXY_TOKEN must stay Cloudflare-managed"


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
    assert 'pip install -e ".[asr-bench]"' in install["run"]

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
