#!/usr/bin/env bash
# review/58 WP6 part B: schedule fallback guard for the LLM producer workflows.
#
# The dispatch Worker's cron starts each producer workflow through `repository_dispatch`
# (config/producer_triggers.yml); the workflow's own `schedule:` stays as a fallback for when that
# trigger is off or fails. Run only on schedule-triggered runs, this decides whether the fallback
# is needed: it writes `skip=true` to $GITHUB_OUTPUT when a repository_dispatch or
# workflow_dispatch run of the same workflow concluded successfully and STARTED less than
# WINDOW_MINUTES ago. The later steps of the job are conditioned on that output, so a skipped run
# ends green.
#
# Schedule-triggered runs never count as recent work: a run this guard skipped also ends
# "success", and counting it would let one skip justify the next forever after the Worker
# trigger stops. Start time, not completion time, is compared so a long dispatched run does not
# hide a missed trigger in the next cadence slot.
#
# Fail-open: any error (API, auth, parse) logs a warning and leaves skip unset, so the run does
# its normal work; the workflow step also sets continue-on-error so even an unexpected failure of
# this script cannot fail or skip the run. Inputs: GH_TOKEN (the job's GITHUB_TOKEN, needs
# `actions: read`), GITHUB_REPOSITORY, GITHUB_REF_NAME, WORKFLOW_FILE (e.g. tag.yml) and
# WINDOW_MINUTES.
set -o pipefail

workflow="${WORKFLOW_FILE:-}"
window="${WINDOW_MINUTES:-}"
repository="${GITHUB_REPOSITORY:-}"
# Only runs on the scheduled run's own branch (the default branch) count: a manual run on a
# feature branch did not do the default branch's work.
branch="${GITHUB_REF_NAME:-}"
if [[ ! "$workflow" =~ ^[a-z0-9][a-z0-9-]*\.yml$ ]] || [[ ! "$window" =~ ^[1-9][0-9]*$ ]] \
  || [[ ! "$repository" =~ ^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$ ]] || [[ -z "$branch" ]]; then
  echo "::warning::schedule guard misconfigured (WORKFLOW_FILE/WINDOW_MINUTES/GITHUB_REPOSITORY/GITHUB_REF_NAME); running normally"
  exit 0
fi
err="${RUNNER_TEMP:-/tmp}/schedule-guard.err"
# Minutes since the most recent successful dispatched run started, or empty when there is none.
filter='[.workflow_runs[]
  | select(.event == "repository_dispatch" or .event == "workflow_dispatch")
  | select(.run_started_at != null)
  | ((now - (.run_started_at | fromdateiso8601)) / 60 | floor)]
  | min // empty'
if ! age=$(gh api -X GET "repos/${repository}/actions/workflows/${workflow}/runs" \
  -f status=success -f branch="$branch" -f per_page=30 --jq "$filter" 2>"$err"); then
  echo "::warning::schedule guard could not list runs ($(head -c 300 "$err")); running normally"
  exit 0
fi

if [[ -z "$age" ]]; then
  echo "schedule guard: no successful dispatched ${workflow} run found; running as the fallback"
  exit 0
fi
if [[ ! "$age" =~ ^-?[0-9]+$ ]]; then
  echo "::warning::schedule guard got an unexpected age '${age}'; running normally"
  exit 0
fi
if (( age < window )); then
  echo "::notice::Skipping this scheduled run: a dispatched ${workflow} run succeeded and started ${age} min ago (window ${window} min)."
  echo "skip=true" >> "$GITHUB_OUTPUT"
else
  echo "schedule guard: last successful dispatched ${workflow} run started ${age} min ago (window ${window} min); running as the fallback"
fi
