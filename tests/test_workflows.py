"""Static contract tests for the GitHub Actions workflows.

These catch wiring mistakes that *no* build or unit test can surface, because the runtime fails
open. Case in point: graceful yield (``_newer_run_queued``) polls the Actions API and silently
returns False on any error — so a missing ``actions: read`` permission or an un-passed
``GITHUB_TOKEN`` disables it with zero signal (the build just runs to its wall-clock deadline).
Asserting the workflow wiring here fails the PR's ``test`` job the moment that regresses.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

_PINNED_SHA = re.compile(r"@[0-9a-f]{40}(?:\s|$)")

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _job(workflow_file: str, job_name: str | None = None) -> tuple[dict, dict]:
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


@pytest.mark.parametrize(
    ("workflow", "job_name", "step_name"),
    [
        ("audio.yml", "audio", "Audio (shard ${{ matrix.shard }}/4)"),
        ("deploy.yml", "build-deploy", "Render feeds"),
        ("tag.yml", "tag", "Produce bounded LLM topic-tag candidates"),
        ("audit.yml", "audit", "Run audit"),
        ("contracts.yml", "contracts", "Probe endpoints + reconcile issues"),
        ("availability-digest.yml", "digest", "Build availability digest"),
    ],
)
def test_provider_fetch_workflows_wire_both_worker_fallbacks(workflow, job_name, step_name):
    """Every workflow that can fetch Granicus/Swagit uses the same recovery configuration."""
    _wf, job = _job(workflow, job_name)
    step = next(item for item in job["steps"] if item.get("name") == step_name)
    env = step.get("env") or {}
    for provider in ("GRANICUS", "SWAGIT"):
        for suffix in ("PROXY_BASE_URL", "PROXY_TOKEN"):
            secret = f"{provider}_{suffix}"
            assert env.get(secret) == f"${{{{ secrets.{secret} }}}}"


def test_stale_commands_are_authorized_review_prs_from_fresh_main():
    wf, job = _job("stale-commands.yml", "lifecycle-pr")
    assert set(_on(wf)) == {"issue_comment"}
    assert wf["permissions"] == {}
    assert wf["concurrency"] == {
        "group": "stale-command-${{ github.event.issue.number }}",
        "cancel-in-progress": False,
    }
    condition = job["if"]
    assert "OWNER" in condition and "MEMBER" in condition and "COLLABORATOR" in condition
    assert "github.event.issue.pull_request == null" in condition
    assert "startsWith(github.event.comment.body, '/stale ')" in condition
    assert job["permissions"] == {
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }

    checkout = next(step for step in job["steps"] if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["ref"] == "main"
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["persist-credentials"] is False

    prepare = next(
        step for step in job["steps"] if step.get("name") == "Validate command and prepare YAML"
    )
    assert prepare["env"]["EVENT_PATH"] == "${{ github.event_path }}"
    assert "scripts/stale_commands.py" in prepare["run"]
    assert "collaborators/$ACTOR/permission" in prepare["run"]
    assert "--permission actor-permission.json" in prepare["run"]
    assert "github.event.comment.body" not in prepare["run"]

    publish = next(
        step for step in job["steps"] if step.get("name") == "Open or update lifecycle PR"
    )
    run = publish["run"]
    assert "pytest -q tests/test_config.py tests/test_stale_commands.py" in run
    assert "gh pr list --head" in run
    assert "gh pr create --head" in run
    assert "git push --force-with-lease" in run
    assert "HEAD:$BRANCH" in run
    assert "HEAD:main" not in run


def test_issue_command_workflows_share_exact_repository_permission_gate():
    for workflow_file, job_name, script in (
        ("stale-commands.yml", "lifecycle-pr", "scripts/stale_commands.py"),
        ("r12-commands.yml", "command", "scripts/r12_commands.py"),
    ):
        _wf, job = _job(workflow_file, job_name)
        command_step = next(step for step in job["steps"] if script in str(step.get("run", "")))
        run = command_step["run"]
        assert "collaborators/$ACTOR/permission" in run
        assert "--permission actor-permission.json" in run


# H6b split the combined enrich into two sharded, lane-pinned workflows.
# Third element is the job name within the workflow file (audio.yml has a wait-for-contracts
# pre-job so the heavy job must be addressed by name, not by position).
HEAVY_WORKFLOWS = [("audio.yml", "audio", "audio"), ("asr.yml", "transcribe", "asr")]


def test_workflows_use_node24_cache_actions_without_force_flag():
    """actions/cache v5 runs on Node 24; the old force flag should not linger. Cache actions are
    SHA-pinned to the current v5 tip (review/22 / GH#734), not the movable @v5 tag."""
    workflow_text = "\n".join(path.read_text() for path in WORKFLOWS.glob("*.yml"))
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24" not in workflow_text
    assert "actions/cache@v4" not in workflow_text
    assert "actions/cache/restore@v4" not in workflow_text
    # SHA-pinned with a `# v5` readability comment; the bare movable tag must not linger.
    assert "actions/cache@caa296126883cff596d87d8935842f9db880ef25 # v5" in workflow_text
    assert "actions/cache@v5" not in workflow_text


def test_city_discovery_llm_route_is_committed_task_config_not_repo_variables():
    workflow = (WORKFLOWS / "city-discovery.yml").read_text()
    site_path = Path(__file__).resolve().parents[1] / "config" / "site_config.yml"
    site = yaml.safe_load(site_path.read_text())

    assert "vars.LLM_MODEL" not in workflow
    assert "vars.LLM_MODE" not in workflow
    assert site["city_discovery"] == {
        "llm_model": "gemini/gemini-3-flash-preview",
        "llm_mode": "direct",
    }


def test_city_discovery_defers_invalid_model_output_but_surfaces_unexpected_failures():
    workflow = (WORKFLOWS / "city-discovery.yml").read_text()

    assert workflow.count("DISCOVERY_DEFERRED=75") == 2
    assert workflow.count('if [ "$status" -eq "$DISCOVERY_DEFERRED" ]; then') == 2
    assert workflow.count("failures=$((failures + 1))") == 2
    assert workflow.count('if [ "$failures" -ne 0 ]; then') >= 2


@pytest.mark.parametrize(
    "workflow,environment,secret_names",
    [
        ("modal-deploy.yml", "modal-production", {"MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET"}),
        ("beam-deploy.yml", "beam-production", {"BEAM_TOKEN"}),
    ],
)
def test_external_worker_deploys_use_secret_scoped_environments(
    workflow, environment, secret_names
):
    """Provider credentials are stored as environment secrets, not repository secrets."""
    _wf, job = _job(workflow, "deploy")
    assert job.get("environment") == environment
    env = job.get("env", {})
    for name in secret_names:
        assert env.get(name) == f"${{{{ secrets.{name} }}}}"


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
    step = next(
        s
        for s in job["steps"]
        if "citypods enrich" in str(s.get("run", ""))
        or "citypods compute run-internal-worker" in str(s.get("run", ""))
    )
    token_sources = {**(job.get("env") or {}), **(step.get("env") or {})}
    assert "GITHUB_TOKEN" in token_sources, (
        f"{workflow} must pass GITHUB_TOKEN to the heavy phase; Actions does not auto-export it"
    )


@pytest.mark.parametrize("workflow,lane,job_name", HEAVY_WORKFLOWS)
def test_heavy_workflow_is_sharded_and_lane_pinned(workflow, lane, job_name):
    """Each heavy workflow runs a source-sharded matrix pinned to one lane, so concurrent shards own
    disjoint record files (scoped push_state) and the two ASR models never co-load."""
    wf, job = _job(workflow, job_name)
    matrix = job.get("strategy", {}).get("matrix", {})
    if workflow == "audio.yml":
        shards = matrix.get("shard")
        assert "fromJSON(needs.plan.outputs.matrix).shard" in str(shards)
    else:
        shards = matrix.get("slot")
        assert shards == [0, 1, 2, 3], f"{workflow} must run four identical pull workers"
    step = next(
        s
        for s in job["steps"]
        if "citypods enrich" in str(s.get("run", ""))
        or "citypods compute run-internal-worker" in str(s.get("run", ""))
    )
    run = str(step["run"])
    if workflow == "audio.yml":
        assert f"--lane {lane}" in run, f"{workflow} must pin --lane {lane}"
        assert "--shard ${{ matrix.shard }}/4" in run, (
            f"{workflow} must pass --shard <matrix.shard>/4 matching the canonical plan"
        )
    else:
        assert "citypods compute run-internal-worker" in run
        assert "CITYPODS_INTERNAL_WORKER_SLOT" in str(step.get("env", {}))


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
    step = next(
        s
        for s in job["steps"]
        if "citypods enrich" in str(s.get("run", ""))
        or "citypods compute run-internal-worker" in str(s.get("run", ""))
    )
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


def test_asr_reconcile_scopes_storage_secrets_to_the_step_that_needs_them():
    # CR2-GH-16/MR-GH-02: checkout/setup-python/install need no credentials.
    _wf, reconcile = _job("asr.yml", job_name="reconcile")
    assert "env" not in reconcile or not any(
        k.startswith(("B2_", "R2_", "CLOUDFLARE_")) for k in (reconcile.get("env") or {})
    )
    reconcile_step = next(
        s
        for s in reconcile["steps"]
        if s.get("name") == "Rebuild work index + reconcile dispatch leases + free-tier budget"
    )
    for var in ("B2_ENDPOINT", "R2_ACCESS_KEY_ID"):
        assert var in reconcile_step.get("env", {})
    for step in reconcile["steps"]:
        if step is reconcile_step:
            continue
        assert not any(k.startswith(("B2_", "R2_", "CLOUDFLARE_")) for k in (step.get("env") or {}))


def test_asr_uses_identical_pull_workers():
    _wf, reconcile = _job("asr.yml", job_name="reconcile")
    _wf, asr = _job("asr.yml", job_name="asr")

    reconcile_runs = "\n".join(str(step.get("run", "")) for step in reconcile["steps"])
    assert "citypods compute reconcile" in reconcile_runs
    assert "plan-shards" not in reconcile_runs
    assert not any("upload-artifact" in step.get("uses", "") for step in reconcile["steps"])
    assert not any("download-artifact" in step.get("uses", "") for step in asr["steps"])
    run_step = next(
        step
        for step in asr["steps"]
        if "citypods compute run-internal-worker" in str(step.get("run", ""))
    )
    assert "CITYPODS_INTERNAL_WORKER_SLOT" in run_step.get("env", {})


def test_asr_quality_eval_workflow_is_separate_and_uploads_artifacts():
    wf, job = _job("asr-quality-eval.yml", job_name="evaluate")
    triggers = _on(wf)
    assert set(triggers) >= {"schedule", "workflow_dispatch"}
    assert wf.get("concurrency", {}).get("group") == "asr-quality-eval"
    assert wf.get("concurrency", {}).get("cancel-in-progress") is False
    sample_step = next(
        step for step in job["steps"] if "transcript-quality sample" in str(step.get("run", ""))
    )
    assert "transcript-quality evaluate" in sample_step["run"]
    upload = next(
        step for step in job["steps"] if "actions/upload-artifact@" in str(step.get("uses", ""))
    )
    assert upload["with"]["retention-days"] == 14

    install = next(step for step in job["steps"] if step.get("name") == "Install")
    assert "asr-align2" in install["run"]
    assert "-c constraints/asr.txt" in install["run"]  # single lock file, no separate one
    cache = next(
        step for step in job["steps"] if step.get("name") == "Cache L2 CTC aligner model (MMS_FA)"
    )
    assert cache["with"]["path"] == "~/.cache/torch/hub/checkpoints"
    assert "actions/cache@" in cache["uses"]


def test_asr_quality_review_workflow_is_weekly_issue_packaging():
    wf, job = _job("asr-quality-review.yml", job_name="review")
    triggers = _on(wf)
    assert set(triggers) >= {"schedule", "workflow_dispatch"}
    assert wf.get("permissions", {}).get("issues") == "write"
    package = next(
        step
        for step in job["steps"]
        if "transcript-quality package-review" in str(step.get("run", ""))
    )
    assert "package-review" in package["run"]
    publish = next(
        step for step in job["steps"] if step.get("name") == "Open or update H15 review issues"
    )
    assert "gh issue create" in publish["run"]
    assert "gh issue edit" in publish["run"]


def test_asr_quality_ingest_workflow_is_event_driven():
    wf, resolve_job = _job("asr-quality-ingest.yml", job_name="resolve")
    triggers = _on(wf)
    assert set(triggers) >= {"issues", "issue_comment", "schedule", "workflow_dispatch"}
    # Deny-all at the workflow level; each job grants only what it actually needs (resolve only
    # ever reads issues — no checkout, no writes).
    assert wf["permissions"] == {}
    assert resolve_job["permissions"] == {"issues": "read"}
    resolve = next(
        step for step in resolve_job["steps"] if step.get("name") == "Resolve candidate issue(s)"
    )
    assert "EVENT_COMMENT_BODY" in resolve.get("env", {})
    assert "/h15-ingest" in resolve["run"]

    ingest_job = wf["jobs"]["ingest"]
    assert ingest_job["needs"] == "resolve"
    assert ingest_job["permissions"] == {"contents": "read", "issues": "write"}
    # A single job processing the full resolved list sequentially -- not a matrix leg per issue.
    # A matrix here previously gave each leg the *complete* NUMBERS list (every leg reran the
    # whole loop), duplicating comments/closes/record writes across N concurrent legs for N
    # resolved issues.
    assert "strategy" not in ingest_job
    ingest = next(
        step
        for step in ingest_job["steps"]
        if "transcript-quality ingest-review" in str(step.get("run", ""))
    )
    assert "--issue-body-file" in ingest["run"]
    assert "gh issue view" in ingest["run"]
    assert ".stored == true" in ingest["run"]
    stored_branch = ingest["run"].split(".stored == true")[1].split("else")[0]
    assert "<!-- h15-ingest:" in stored_branch
    assert "state,comments" in stored_branch
    assert "--body-file" in stored_branch
    assert '.state == "OPEN"' in stored_branch
    assert "gh issue comment" in stored_branch
    assert "gh issue close" in stored_branch


def test_llm_tag_review_ingest_workflow_is_event_driven_and_guards_stored():
    wf, resolve_job = _job("llm-tag-review-ingest.yml", job_name="resolve")
    triggers = _on(wf)
    assert set(triggers) >= {"issues", "issue_comment", "schedule", "workflow_dispatch"}
    assert wf["permissions"] == {}
    assert resolve_job["permissions"] == {"issues": "read"}
    resolve = next(
        step for step in resolve_job["steps"] if step.get("name") == "Resolve review issues"
    )
    assert "EVENT_COMMENT_BODY" in resolve.get("env", {})
    assert "/llm-ingest" in resolve["run"]

    ingest_job = wf["jobs"]["ingest"]
    assert ingest_job["needs"] == "resolve"
    assert ingest_job["permissions"] == {"contents": "read", "issues": "write"}
    assert "strategy" not in ingest_job
    ingest = next(
        step for step in ingest_job["steps"] if "llm-evaluation ingest" in str(step.get("run", ""))
    )
    assert "--issue-body-file" in ingest["run"]
    assert "gh issue view" in ingest["run"]
    assert ".stored == true" in ingest["run"]
    stored_branch = ingest["run"].split(".stored == true")[1].split("else")[0]
    assert "<!-- llm-ingest:" in stored_branch
    assert "state,comments" in stored_branch
    assert "--body-file" in stored_branch
    assert '.state == "OPEN"' in stored_branch
    assert "gh issue comment" in stored_branch
    assert "gh issue close" in stored_branch


def test_asr_quality_ingest_schedule_fallback_scans_open_children():
    """The safety-net cron for missed issues.edited/issue_comment webhooks must actually scan
    open H15 child issues, not just resolve to an empty issue list and skip everything."""
    wf, resolve_job = _job("asr-quality-ingest.yml", job_name="resolve")
    resolve = next(
        step for step in resolve_job["steps"] if step.get("name") == "Resolve candidate issue(s)"
    )
    assert 'EVENT_NAME" = "schedule"' in resolve["run"]
    assert "gh issue list" in resolve["run"]
    assert "H15 sample" in resolve["run"]

    finalize_job = wf["jobs"]["finalize"]
    assert set(finalize_job["needs"]) == {"resolve", "ingest"}
    assert finalize_job["if"] == (
        "always() && needs.resolve.result != 'failure' && needs.resolve.outputs.numbers != '[]'"
    )
    assert finalize_job["permissions"] == {"issues": "write"}  # never checks out code
    close_parent = next(
        step
        for step in finalize_job["steps"]
        if step.get("name") == "Close parent issues when their batch is clear"
    )
    assert close_parent["env"]["GH_REPO"] == "${{ github.repository }}"
    # Native GitHub hierarchy, rather than the retired body-text convention. The query comes
    # directly from each parent, so issue numbers such as #5/#50 cannot collide.
    assert "--json subIssues" in close_parent["run"]
    # gh's --json subIssues returns a GraphQL connection object ({nodes, totalCount}), not a
    # flat array -- see constraints/gh-cli.txt for the 2026-07-27 incident this was fixed from.
    assert ".subIssues.nodes[]" in close_parent["run"]
    assert "Parent issue: #" not in close_parent["run"]


def test_audio_lane_needs_no_whisper():
    """The audio lane never runs ASR, so it must not install the asr extra or download Whisper —
    that's wasted runner time and memory."""
    _wf, job = _job("audio.yml", job_name="audio")
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "prepare_whisper" not in runs
    assert '".[asr' not in runs and "[asr,storage]" not in runs


def test_audio_workflow_uploads_shard_evidence_and_builds_h16_report():
    wf, audio = _job("audio.yml", job_name="audio")
    _wf, validate = _job("audio.yml", job_name="validate-h16")

    assert validate["needs"] == ["plan", "audio", "audio-no-op"]
    assert validate["if"] == "always()"
    assert wf["permissions"]["actions"] == "read"
    assert audio["if"] == "needs.plan.outputs.has_work == 'true'"
    plan = wf["jobs"]["plan"]
    assert plan["outputs"]["matrix"] == "${{ steps.plan.outputs.matrix }}"
    plan_step = next(step for step in plan["steps"] if step.get("id") == "plan")
    assert "--restore-state" in plan_step["run"]
    assert "--matrix-output audio-plan/matrix.json" in plan_step["run"]
    assert "actions/upload-artifact@" in str(plan["steps"])
    planner_install = next(step for step in plan["steps"] if step.get("name") == "Install planner")
    assert 'pip install -e ".[storage]"' in planner_install["run"]
    assert "state-snapshot-restored" in " ".join(str(s.get("run", "")) for s in audio["steps"])

    upload = next(
        step for step in audio["steps"] if step.get("name") == "Upload H16 shard evidence"
    )
    assert upload["if"] == "always()"
    assert (
        upload["with"]["name"]
        == "audio-h16-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.shard }}"
    )
    assert upload["with"]["path"] == "h16-evidence/*.json"
    assert upload["with"]["retention-days"] == 14
    collect = next(step for step in audio["steps"] if step.get("name") == "Collect H16 run event")
    assert "PYTHONPATH=. python scripts/scan_h16_log.py" in collect["run"]

    download = next(
        step for step in validate["steps"] if step.get("name") == "Download H16 shard evidence"
    )
    assert download["continue-on-error"] is True
    assert (
        download["with"]["pattern"] == "audio-h16-${{ github.run_id }}-${{ github.run_attempt }}-*"
    )
    assert download["with"]["merge-multiple"] is True

    report = next(
        step for step in validate["steps"] if step.get("name") == "Build H16 acceptance report"
    )
    assert "citypods h16-report" in report["run"]
    assert "needs.plan.outputs.shard_count || '0'" in report["run"]
    assert "mkdir -p h16-input" in report["run"]
    assert '>> "$GITHUB_STEP_SUMMARY"' in report["run"]

    report_upload = next(
        step for step in validate["steps"] if step.get("name") == "Upload H16 acceptance report"
    )
    assert report_upload["if"] == "always()"
    assert report_upload["with"]["retention-days"] == 30

    # validate-h16 runs repository code (pip install -e .), so its setup actions must be
    # SHA-pinned and the checkout must not persist the GITHUB_TOKEN into .git/config.
    checkout = next(
        step for step in validate["steps"] if "actions/checkout@" in step.get("uses", "")
    )
    assert _PINNED_SHA.search(checkout["uses"]), checkout["uses"]
    assert checkout["with"]["persist-credentials"] is False
    setup_python = next(
        step for step in validate["steps"] if "actions/setup-python@" in step.get("uses", "")
    )
    assert _PINNED_SHA.search(setup_python["uses"]), setup_python["uses"]


def test_checkout_and_setup_python_are_sha_pinned_everywhere():
    """Blanket supply-chain policy: every checkout/setup-python reference is pinned to a full
    commit SHA, not a floating ``@v6`` tag. Pinning one job while leaving siblings floating gives
    a false sense of safety, so this guards the whole workflow directory."""
    offenders = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for line in workflow.read_text().splitlines():
            stripped = line.strip()
            if "actions/checkout@" in stripped or "actions/setup-python@" in stripped:
                if not _PINNED_SHA.search(stripped):
                    offenders.append(f"{workflow.name}: {stripped}")
    assert not offenders, "unpinned action references:\n" + "\n".join(offenders)


def test_checkout_disables_credential_persistence_everywhere():
    """Every job checks out the repo and then executes its code (pip install -e ., npm test),
    yet none push via the persisted GITHUB_TOKEN (state lands in B2/R2; Pages deploys via
    actions/deploy-pages OIDC). So no checkout needs persisted credentials — assert they are all
    disabled so a compromised setup.py/package script can't read the token."""
    offenders = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        wf = yaml.safe_load(workflow.read_text())
        for job_name, job in (wf.get("jobs") or {}).items():
            for step in job.get("steps", []):
                if "actions/checkout@" in str(step.get("uses", "")):
                    if (step.get("with") or {}).get("persist-credentials") is not False:
                        offenders.append(f"{workflow.name}:{job_name}")
    assert not offenders, "checkout without persist-credentials: false:\n" + "\n".join(offenders)


def test_audio_uses_pinned_runner_image_with_verified_host_fallback():
    wf, job = _job("audio.yml", job_name="audio")
    env = job["env"]
    image = env["AUDIO_RUNNER_IMAGE"]
    assert image.startswith("ghcr.io/bashfulbits/citypods-audio-runner:")
    assert not image.endswith(":latest")
    assert len(env["FFMPEG_SHA256"]) == 64
    assert wf["permissions"]["packages"] == "read"
    image_wf, _ = _job("audio-runner-image.yml", job_name="build")
    assert image == image_wf["env"]["IMAGE"]

    select = next(s for s in job["steps"] if s.get("name") == "Select audio runtime")
    # Self-built ffmpeg (scripts/build_ffmpeg_static.sh) vendored to our own B2 bucket, not
    # fetched from an upstream/mirror host (review/22). FFMPEG_URL is built from the B2 secret,
    # scoped to this step since it's the only one that needs it.
    assert select["env"]["FFMPEG_URL"] == (
        "${{ secrets.B2_PUBLIC_BASE_URL }}/deps/ffmpeg/7.1.5/ffmpeg-7.1.5-linux64-static.tar.xz"
    )
    assert 'timeout 300 docker pull "${AUDIO_RUNNER_IMAGE}"' in select["run"]
    assert "import boto3, citypods" in select["run"]
    assert "timeout 300 python scripts/install_static_ffmpeg.py" in select["run"]
    assert '--sha256 "${FFMPEG_SHA256}"' in select["run"]
    assert '"${FFMPEG_FALLBACK_DIR}/bin/ffprobe" -version' in select["run"]
    assert "poppler-utils tesseract-ocr" in select["run"]
    assert "pdftocairo -v" in select["run"]
    assert "tesseract --version" in select["run"]
    # Runtime (non -dev) packages for the codec/TLS libraries build_ffmpeg_static.sh links
    # dynamically -- only needed on the host fallback path, not the container path.
    assert "libgnutls30 libopus0 libvpx9 libdav1d7 libmp3lame0" in select["run"]

    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
    assert "docker run --rm --init" in runs
    assert "python -m citypods.cli enrich --lane audio" in runs
    audio_step = next(s for s in job["steps"] if "citypods enrich" in str(s.get("run", "")))
    # CR-GH-25: storage/Granicus secrets are scoped to this step (the only one that touches
    # them), not the whole job -- checkout/setup-python/cache/runtime-select steps don't need them.
    step_env = audio_step.get("env") or {}
    assert step_env["GRANICUS_PROXY_BASE_URL"] == "${{ secrets.GRANICUS_PROXY_BASE_URL }}"
    assert step_env["GRANICUS_PROXY_TOKEN"] == "${{ secrets.GRANICUS_PROXY_TOKEN }}"
    assert "--env GRANICUS_PROXY_BASE_URL" in audio_step["run"]
    assert "--env GRANICUS_PROXY_TOKEN" in audio_step["run"]


def test_audio_runner_image_build_is_scheduled_and_publishes_ghcr():
    wf, job = _job("audio-runner-image.yml", job_name="build")
    assert {"schedule", "workflow_dispatch", "push"} <= set(_on(wf))
    assert wf["permissions"]["packages"] == "write"
    assert job["timeout-minutes"] == 30
    assert wf["env"]["IMAGE"].endswith(":py312-ffmpeg71-v2")
    assert not wf["env"]["IMAGE"].endswith(":latest")

    build = next(s for s in job["steps"] if "docker/build-push-action" in s.get("uses", ""))
    # CR-GH-12: push is gated to main so a manual dispatch from a feature branch never
    # overwrites the shared GHCR tag.
    assert build["with"]["push"] == "${{ github.ref == 'refs/heads/main' }}"
    assert build["with"]["platforms"] == "linux/amd64"
    assert build["with"]["file"] == ".github/audio-runner/Dockerfile"
    build_args = build["with"]["build-args"]
    assert "FFMPEG_SHA256=" in build_args
    assert "FFMPEG_URL=${{ secrets.B2_PUBLIC_BASE_URL }}/deps/ffmpeg/" in build_args
    assert _step_index(job, "ffprobe -version") > _step_index(job, "docker/build-push-action")

    _audio_wf, audio_job = _job("audio.yml", job_name="audio")
    select = next(s for s in audio_job["steps"] if s.get("name") == "Select audio runtime")
    # Self-built ffmpeg (scripts/build_ffmpeg_static.sh) vendored to our own B2 bucket, not
    # fetched from an upstream/mirror host -- same pin everywhere (review/22). Here it's inlined
    # directly into build-args (single-job workflow) rather than cross-referenced via an env var
    # the way the shell-script consumers do it, so check it's a substring, not an exact env match.
    assert select["env"]["FFMPEG_URL"] in build_args
    assert audio_job["env"]["FFMPEG_SHA256"] == wf["env"]["FFMPEG_SHA256"]

    dockerfile = (
        Path(__file__).resolve().parents[1] / ".github" / "audio-runner" / "Dockerfile"
    ).read_text()
    # Ubuntu noble, not the official python:3.12-slim (Debian bookworm) image -- must match the
    # ubuntu-latest (also noble) host build_ffmpeg_static.sh runs on, since the vendored binary's
    # dynamically-linked codec libs (libvpx9/libdav1d7 below) are SONAME-specific to that distro.
    assert "FROM ubuntu:24.04" in dockerfile
    # Runtime (non -dev) packages for build_ffmpeg_static.sh's dynamically-linked codec/TLS
    # libs -- not apt-get-installed ffmpeg itself.
    assert "libgnutls30 libopus0 libvpx9 libdav1d7 libmp3lame0" in dockerfile
    assert "apt-get install -y ffmpeg" not in dockerfile


def test_asr_uses_verified_static_ffmpeg_without_baking_whisper_weights():
    _wf, job = _job("asr.yml", job_name="asr")
    env = job["env"]
    image_wf, image_job = _job("audio-runner-image.yml", job_name="build")
    build = next(s for s in image_job["steps"] if "docker/build-push-action" in s.get("uses", ""))

    assert env["FFMPEG_SHA256"] == image_wf["env"]["FFMPEG_SHA256"]
    assert len(env["FFMPEG_SHA256"]) == 64

    install = next(s for s in job["steps"] if s.get("name") == "Install verified static ffmpeg")
    run = install["run"]
    # Same ffmpeg pin everywhere (review/22).
    assert install["env"]["FFMPEG_URL"] in build["with"]["build-args"]
    assert "timeout 300 python scripts/install_static_ffmpeg.py" in run
    assert '--sha256 "${FFMPEG_SHA256}"' in run
    assert '"${FFMPEG_DIR}/bin/ffmpeg" -version' in run
    assert '"${FFMPEG_DIR}/bin" >> "$GITHUB_PATH"' in run
    assert "libgnutls30 libopus0 libvpx9 libdav1d7 libmp3lame0" in run

    runs = "\n".join(str(s.get("run", "")) for s in job["steps"])
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

    # `durations` is a free-form string input; must be passed via env, never inlined into
    # `run:` shell text (script-injection guard — the other inputs used inline here are
    # GitHub-validated `number`s, so they can't carry shell metacharacters).
    assert "${{ inputs.durations }}" not in runs
    sustained_step = next(
        step for step in job["steps"] if step.get("name") == "Run sustained Granicus matrix"
    )
    assert sustained_step["env"]["DURATIONS"] == "${{ inputs.durations }}"
    assert '--durations "$DURATIONS"' in sustained_step["run"]

    upload = next(step for step in job["steps"] if "upload-artifact" in step.get("uses", ""))
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "granicus-*-results.json"


def test_spike_r2_cas_has_a_concurrency_group():
    # CR2-GH-13: no concurrency block meant overlapping manual dispatches could consume
    # duplicate runner time.
    wf, _job_dict = _job("spike-r2-cas.yml")
    assert wf.get("concurrency", {}).get("cancel-in-progress") is True
    assert wf["concurrency"]["group"]


def test_ci_has_a_concurrency_group():
    # MR-GH-05: ci.yml runs on every pull_request push with no concurrency group, so rapid
    # pushes ran overlapping CI to completion instead of canceling the superseded run.
    wf, _job_dict = _job("ci.yml")
    assert wf.get("concurrency", {}).get("cancel-in-progress") is True
    assert "${{ github.ref }}" in wf["concurrency"]["group"]


def test_ci_runs_granicus_worker_unit_tests():
    _wf, job = _job("ci.yml", job_name="test")
    step = next(
        step for step in job["steps"] if step.get("name") == "Test Granicus Cloudflare Worker"
    )
    assert step["working-directory"] == "workers/granicus-media-proxy"
    assert step["run"] == "npm test"


def test_swagit_worker_credentials_reach_provider_fetch_lanes():
    for workflow, job_name, step_name in (
        ("tag.yml", "tag", "Produce bounded LLM topic-tag candidates"),
        ("audio.yml", "audio", "Audio (shard ${{ matrix.shard }}/4)"),
    ):
        _wf, job = _job(workflow, job_name=job_name)
        step = next(step for step in job["steps"] if step.get("name") == step_name)
        assert step["env"]["SWAGIT_PROXY_BASE_URL"] == "${{ secrets.SWAGIT_PROXY_BASE_URL }}"
        assert step["env"]["SWAGIT_PROXY_TOKEN"] == "${{ secrets.SWAGIT_PROXY_TOKEN }}"
        if workflow == "audio.yml":
            assert "--env SWAGIT_PROXY_BASE_URL" in step["run"]
            assert "--env SWAGIT_PROXY_TOKEN" in step["run"]


def test_tag_lane_uses_async_llm_dispatch_and_keeps_provider_key_off_runner():
    _wf, job = _job("tag.yml", job_name="tag")
    step = next(
        step
        for step in job["steps"]
        if step.get("name") == "Produce bounded LLM topic-tag candidates"
    )
    env = step["env"]
    assert env["LLM_DISPATCH_URL"] == "${{ secrets.LLM_DISPATCH_URL }}"
    assert env["LLM_DISPATCH_AUTH_TOKEN"] == "${{ secrets.LLM_DISPATCH_AUTH_TOKEN }}"
    assert "GEMINI_API_KEY" not in env

    site = yaml.safe_load((WORKFLOWS.parent.parent / "config" / "site_config.yml").read_text())
    assert site["tagging"]["llm_mode"] == "dispatch"


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
    # SHA-pinned per review/22 / GH#734 (the `# v3` comment is stripped by the YAML parser).
    assert deploy["uses"] == "cloudflare/wrangler-action@9acf94ace14e7dc412b076f2c5c20b8ce93c79cd"
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
    render = _step_index(job, "citypods build --phase render --no-refresh")
    deploy = _step_index(job, "actions/deploy-pages")
    assert render >= 0 and deploy >= 0, "render and deploy steps required"
    assert render < deploy, "deploy.yml must render before deploying"
    # The Pages plumbing stays on deploy, not enrich.
    assert "actions/upload-pages-artifact" in uses and "actions/deploy-pages" in uses


def test_deploy_scopes_storage_secrets_to_render_step_only():
    """CR2-GH-16/MR-GH-02: B2/R2 secrets must not sit in job-level env where checkout/
    setup-python/cache/configure-pages/upload-pages-artifact/deploy-pages steps (none of which
    touch storage) can see them — only the render step (which pulls durable state) needs them."""
    wf, job = _job("deploy.yml")
    assert "env" not in job or not any(
        k.startswith(("B2_", "R2_", "CLOUDFLARE_")) for k in (job.get("env") or {})
    )
    render = next(
        s for s in job["steps"] if s.get("run") == "citypods build --phase render --no-refresh"
    )
    env = render.get("env", {})
    for var in ("B2_ENDPOINT", "B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "B2_PUBLIC_BASE_URL"):
        assert var in env, f"deploy.yml's render step is missing {var}"
    for var in ("CLOUDFLARE_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
        assert var in env, f"deploy.yml's render step is missing {var}"
    non_render_steps = [s for s in job["steps"] if s is not render]
    assert not any(
        k.startswith(("B2_", "R2_", "CLOUDFLARE_"))
        for s in non_render_steps
        for k in (s.get("env") or {})
    )


def test_deploy_job_has_a_timeout():
    # CR2-GH-16: a stalled render/deploy must not hold the `pages` concurrency slot for hours.
    _wf, job = _job("deploy.yml")
    assert job.get("timeout-minutes")


def test_deploy_resource_report_does_not_block_deploy_on_failure():
    # CR2-GH-15: a bug in the cosmetic resource-report generator must not block feed validation
    # or the deploy itself.
    _wf, job = _job("deploy.yml")
    report_step = next(s for s in job["steps"] if s.get("name") == "Resource report")
    assert report_step.get("continue-on-error") is True


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


def test_audit_workflow_pulls_canonical_state_not_a_borrowed_cache():
    """audit.yml must not rely on actions/cache's ``build-state-`` restore-key prefix: that
    prefix collides with audio.yml's per-shard caches (``build-state-audio-...``) and
    preview.yml's PR caches (``build-state-preview-...``), so a restore could land on a
    partial/unrelated snapshot and compare an EDL against a served-duration captured at a
    different point in the pipeline's history (the cause of GH#464-478/#490-491's false
    positives). The audit must instead pull the bucket's canonical state directly, same as
    deploy.yml/audio.yml already do, with actions/cache no longer in the picture at all."""
    wf, job = _job("audit.yml")

    assert not any("actions/cache" in str(s.get("uses", "")) for s in job["steps"]), (
        "audit.yml should pull canonical state from the bucket, not restore an actions/cache blob"
    )

    install = next(s for s in job["steps"] if s.get("name") == "Install")
    assert 'pip install -e ".[storage]"' in install["run"]

    run_audit = next(s for s in job["steps"] if s.get("name") == "Run audit")
    env = run_audit.get("env", {})
    for var in ("B2_ENDPOINT", "B2_KEY_ID", "B2_APP_KEY", "B2_BUCKET", "B2_PUBLIC_BASE_URL"):
        assert var in env, f"audit.yml's Run audit step is missing {var} (needed for pull_state)"


def test_audit_workflow_exposes_guarded_timeline_repair_cohort_dispatch():
    wf, job = _job("audit.yml")
    inputs = _on(wf)["workflow_dispatch"]["inputs"]

    assert inputs["timeline_repair"]["type"] == "boolean"
    assert inputs["timeline_repair"]["default"] is False
    assert inputs["timeline_repair_min_delta"]["default"] == "1.0"
    assert inputs["timeline_repair_cohort"]["default"] == ""
    assert inputs["timeline_diagnostics"]["type"] == "boolean"
    assert inputs["timeline_diagnostics"]["default"] is False
    assert inputs["timeline_finding_min_delta"]["default"] == "1.0"

    audit_step = next(s for s in job["steps"] if s.get("name") == "Run audit")
    run = audit_step["run"]
    assert "timeline_diagnostics_requested=false" in run
    assert '"$TIMELINE_DIAGNOSTICS_INPUT" = "true"' in run
    assert "timeline_diagnostics_requested=true" in run
    assert 'if [ "$timeline_diagnostics_requested" = "true" ]; then' in run
    assert "--timeline-diagnostics audit-timeline-integrity.jsonl" in run
    assert "--timeline-finding-min-delta" in run
    assert "--persist-timeline-integrity" in run
    assert "--timeline-repair-min-delta" in run
    assert "--timeline-repair-cohort" in run
    assert '"$GIT_REF" != "refs/heads/main"' in run
    assert "timeline_repair_cohort is required" in run
    # Free-form string inputs must be passed via env, never inlined into `run:` shell text
    # (script-injection guard, same shape as the fixed CR2-GH-07/C1).
    assert "${{ inputs.timeline_repair_min_delta }}" not in run
    assert "${{ inputs.timeline_repair_cohort }}" not in run
    assert "${{ inputs.timeline_finding_min_delta }}" not in run
    step_env = audit_step["env"]
    assert step_env["TIMELINE_REPAIR_MIN_DELTA"] == "${{ inputs.timeline_repair_min_delta }}"
    assert step_env["TIMELINE_REPAIR_COHORT"] == "${{ inputs.timeline_repair_cohort }}"


def test_reset_backoff_workflow_exposes_targeted_hosted_filters():
    wf, job = _job("reset-backoff.yml")
    inputs = _on(wf)["workflow_dispatch"]["inputs"]

    assert inputs["provider"]["default"] == ""
    assert inputs["source"]["default"] == ""
    assert inputs["uid"]["default"] == ""
    assert inputs["error"]["default"] == ""
    assert inputs["include_hosted"]["type"] == "boolean"
    assert inputs["include_hosted"]["default"] is False
    assert inputs["apply"]["type"] == "boolean"
    assert inputs["apply"]["default"] is False

    reset_step = next(s for s in job["steps"] if s.get("name") == "Reset materialization backoff")
    run = reset_step["run"]
    assert 'ARGS+=(--provider "$PROVIDER")' in run
    assert 'ARGS+=(--source "$SOURCE")' in run
    assert 'ARGS+=(--uid "$RECORD_UID")' in run
    assert 'ARGS+=(--error "$ERROR_CODE")' in run
    assert "ARGS+=(--include-hosted)" in run
    assert "ARGS+=(--apply)" in run
    assert '"$GIT_REF" != "refs/heads/main"' in run
    # bash's own readonly UID builtin would shadow an env var literally named UID (CR2-GH-06/C2).
    step_env = reset_step["env"]
    assert "UID" not in step_env
    assert step_env["RECORD_UID"] == "${{ inputs.uid }}"


def test_duration_normalize_workflow_is_manual_bounded_and_archived():
    wf, job = _job("duration-normalize.yml")
    assert set(_on(wf)) == {"workflow_dispatch"}
    inputs = _on(wf)["workflow_dispatch"]["inputs"]

    assert inputs["source"]["default"] == ""
    assert inputs["uid"]["default"] == ""
    assert inputs["max_items"]["default"] == "200"
    assert inputs["probe_existing"]["type"] == "boolean"
    assert inputs["probe_existing"]["default"] is True
    assert inputs["apply"]["type"] == "boolean"
    assert inputs["apply"]["default"] is False
    assert wf["permissions"] == {"contents": "read"}
    assert wf["concurrency"]["group"] == "audio"
    assert wf["concurrency"]["cancel-in-progress"] is False
    assert len(job["env"]["FFMPEG_SHA256"]) == 64

    normalize_step = next(s for s in job["steps"] if s.get("name") == "Normalize durations")
    run = normalize_step["run"]
    assert 'ARGS=(--max-items "$MAX_ITEMS")' in run
    assert 'ARGS+=(--source "$SOURCE")' in run
    assert 'ARGS+=(--uid "$RECORD_UID")' in run
    assert "ARGS+=(--no-probe-existing)" in run
    assert "ARGS+=(--apply)" in run
    assert 'ARGS+=(--ffmpeg-binary "${FFMPEG_DIR}/bin/ffmpeg")' in run
    assert "python scripts/normalize_durations.py" in run
    assert '"$GIT_REF" != "refs/heads/main"' in run
    # bash's own readonly UID builtin would shadow an env var literally named UID.
    step_env = normalize_step["env"]
    assert "UID" not in step_env
    assert step_env["RECORD_UID"] == "${{ inputs.uid }}"

    install = next(s for s in job["steps"] if s.get("name") == "Install pinned ffmpeg")
    # Self-built ffmpeg vendored to our own B2 bucket, not fetched from an upstream/mirror host
    # (review/22). FFMPEG_URL is built from the B2 secret, scoped to this step.
    assert install["env"]["FFMPEG_URL"] == (
        "${{ secrets.B2_PUBLIC_BASE_URL }}/deps/ffmpeg/7.1.5/ffmpeg-7.1.5-linux64-static.tar.xz"
    )
    assert "python scripts/install_static_ffmpeg.py" in install["run"]
    assert '--sha256 "${FFMPEG_SHA256}"' in install["run"]
    assert '"${FFMPEG_DIR}/bin/ffprobe" -version >/dev/null' in install["run"]
    assert "libgnutls30 libopus0 libvpx9 libdav1d7 libmp3lame0" in install["run"]

    upload = next(step for step in job["steps"] if "upload-artifact" in step.get("uses", ""))
    assert upload["if"] == "always()"
    assert "duration-normalize/normalize.jsonl" in upload["with"]["path"]
    assert "duration-normalize/summary.json" in upload["with"]["path"]


def test_clear_materialization_workflow_avoids_injection_and_guards_apply():
    """CR2-GH-07/C1: `run_id` is a free-form string dispatch input; interpolating it directly
    into `run:` shell text is a script-injection shape. MR-GH-01: `apply`/`delete_objects` mutate
    production state and must be refused off `main`."""
    wf, job = _job("clear-materialization.yml")
    inputs = _on(wf)["workflow_dispatch"]["inputs"]
    assert inputs["run_id"]["required"] is True

    step = next(s for s in job["steps"] if s.get("name") == "Clear run materializations")
    run = step["run"]
    assert "${{ inputs.run_id }}" not in run
    assert step["env"]["RUN_ID"] == "${{ inputs.run_id }}"
    assert step["env"]["GIT_REF"] == "${{ github.ref }}"
    assert '"$GIT_REF" != "refs/heads/main"' in run
    assert "ARGS+=(--apply)" in run
    assert "ARGS+=(--delete-objects)" in run
    assert 'python scripts/clear_run_materializations.py "${ARGS[@]}"' in run


def test_reclaim_transcript_workflow_guards_write_to_main():
    wf, job = _job("reclaim-transcript.yml")
    step = next(s for s in job["steps"] if s.get("name") == "Reclaim transcript")
    run = step["run"]
    assert step["env"]["GIT_REF"] == "${{ github.ref }}"
    assert '"$GIT_REF" != "refs/heads/main"' in run
    assert "args+=(--write)" in run
    assert set(_on(wf)) == {"workflow_dispatch"}


def test_no_workflow_inlines_a_free_form_dispatch_input_in_run_text():
    """Generalizes MR-GH-04's ask: broaden the injection guard beyond a single regex for one
    action, to any workflow_dispatch `type: string` (or untyped, which defaults to string) input
    interpolated directly into `run:` shell text rather than passed through `env:`. GitHub
    template-expands `${{ }}` before the shell ever runs, so a free-form string spliced straight
    into script text is a script-injection shape (the same one CR2-GH-07/C1 fixed); `boolean`/
    `number`/`choice` inputs are GitHub-validated and excluded since they can't carry shell
    metacharacters.
    """
    offenders = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        wf = yaml.safe_load(workflow.read_text())
        dispatch = (_on(wf) or {}).get("workflow_dispatch") or {}
        string_inputs = {
            name
            for name, spec in (dispatch.get("inputs") or {}).items()
            if (spec or {}).get("type", "string") == "string"
        }
        if not string_inputs:
            continue
        for job_name, job in (wf.get("jobs") or {}).items():
            for step in job.get("steps", []):
                run_text = str(step.get("run", ""))
                for name in string_inputs:
                    # Matches both `inputs.<name>` and the older `github.event.inputs.<name>` form.
                    if re.search(rf"\binputs\.{re.escape(name)}\b", run_text):
                        offenders.append(f"{workflow.name}:{job_name}: inputs.{name}")
    assert not offenders, "free-form string input inlined in run: shell text:\n" + "\n".join(
        offenders
    )


def test_contracts_wait_loop_polls_both_audio_and_granicus_probe():
    # CR2-GH-19: granicus-probe.yml shares audio.yml's `group: audio` concurrency slot, so a wait
    # loop that only checks audio.yml can proceed to probe Granicus while granicus-probe.yml is
    # actively running — the exact 403-storm scenario this gate exists to prevent.
    _wf, job = _job("contracts.yml")
    wait_step = next(
        s for s in job["steps"] if s.get("name") == "Wait for active audio runs to finish"
    )
    run = wait_step["run"]
    assert "audio.yml granicus-probe.yml" in run or (
        "audio.yml" in run and "granicus-probe.yml" in run
    )


def test_contracts_wait_loop_does_not_merge_stderr_into_the_json_comparison():
    # CR2-GH-18: `2>&1` would merge stderr into the captured value, so a successful call that
    # also emits an incidental stderr warning could corrupt the "[]" comparison.
    _wf, job = _job("contracts.yml")
    wait_step = next(
        s for s in job["steps"] if s.get("name") == "Wait for active audio runs to finish"
    )
    run = wait_step["run"]
    assert "2>&1" not in run


def test_availability_digest_wait_loop_does_not_merge_stderr_into_the_json_comparison():
    _wf, job = _job("availability-digest.yml", job_name="digest")
    wait_step = next(
        s for s in job["steps"] if s.get("name") == "Wait for active audio runs to finish"
    )
    assert "2>&1" not in wait_step["run"]


def test_availability_digest_job_has_a_timeout():
    # CR2-GH-05: a stuck proxy re-fetch/encode must not hold the concurrency slot indefinitely.
    _wf, job = _job("availability-digest.yml", job_name="digest")
    assert job.get("timeout-minutes")


def test_availability_digest_wait_loop_retries_instead_of_masking_gh_failures():
    # CR2-GH-08: a gh API/auth/network error must not be masked as "no active runs" — that would
    # fail-open the exact 403-storm throttle gate GH#300 exists to prevent.
    _wf, job = _job("availability-digest.yml", job_name="digest")
    wait_step = next(
        s for s in job["steps"] if s.get("name") == "Wait for active audio runs to finish"
    )
    run = wait_step["run"]
    assert "2>/dev/null || echo" not in run
    assert "will retry" in run


def test_availability_digest_scopes_secrets_to_steps_that_need_them():
    # CR2-GH-16/MR-GH-02: storage/Granicus-proxy secrets must not sit in job-level env where
    # checkout/setup-python/install/ffmpeg/the wait loop (none of which touch them) can see them.
    _wf, job = _job("availability-digest.yml", job_name="digest")
    assert "env" not in job or not any(
        k.startswith(("B2_", "R2_", "CLOUDFLARE_", "GRANICUS_")) for k in (job.get("env") or {})
    )
    digest_step = next(s for s in job["steps"] if s.get("name") == "Build availability digest")
    for var in ("B2_ENDPOINT", "R2_ACCESS_KEY_ID", "GRANICUS_PROXY_TOKEN"):
        assert var in digest_step.get("env", {})
    push_step = next(s for s in job["steps"] if s.get("name") == "Push digest state")
    assert "B2_ENDPOINT" in push_step.get("env", {})
    storage_steps = {id(digest_step), id(push_step)}
    for step in job["steps"]:
        if id(step) in storage_steps:
            continue
        assert not any(
            k.startswith(("B2_", "R2_", "CLOUDFLARE_", "GRANICUS_"))
            for k in (step.get("env") or {})
        )
