#!/usr/bin/env bash
# Compile hash-pinned constraint files from pyproject.toml in the pinned deploy
# target (linux / CPython 3.12 — the audio-runner base image). Resolving on any
# other OS/Python yields wrong platform wheels, so we always compile inside the
# pinned container. See review/22 and constraints/README.md.
#
# Usage:
#   scripts/compile_constraints.sh            # runs pip-compile inside Docker
#   IN_CONTAINER=1 scripts/compile_constraints.sh   # already in the target env (CI/lock.yml)
set -euo pipefail

# Pinned base digest — keep in sync with .github/audio-runner/Dockerfile.
BASE_IMAGE="python:3.12-slim-bookworm@sha256:76d4b7b6305788c6b4c6a19d6a22a3921bf802e9af4d5e1e5bd771208dba74bf"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ "${IN_CONTAINER:-0}" != "1" ]]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "error: Docker is required to compile constraints for the linux/3.12 target." >&2
    echo "       (Or run inside the target env with IN_CONTAINER=1.)" >&2
    exit 1
  fi
  # The runner's resolver is sometimes broken (the same Azure resolver failure that hangs apt
  # on ubuntu-latest) and a container inherits it. Give the container public resolvers only
  # when this host cannot resolve the package index, so healthy runners and developer
  # machines keep their own resolver.
  dns_args=()
  if command -v getent >/dev/null 2>&1 && ! getent hosts pypi.org >/dev/null 2>&1; then
    echo "::warning::pypi.org does not resolve on this host; giving the container public DNS"
    dns_args=(--dns 1.1.1.1 --dns 8.8.8.8)
  fi
  # Bound the image pull (cached on most runners; a stalled registry read must not run to the
  # job limit). A failed pull is retried once; a second failure fails the gate.
  # (Developer machines without GNU timeout, e.g. stock macOS, skip the bound.)
  pull_timeout=()
  if command -v timeout >/dev/null 2>&1; then
    pull_timeout=(timeout 180)
  elif command -v gtimeout >/dev/null 2>&1; then
    pull_timeout=(gtimeout 180)
  fi
  if ! ${pull_timeout[@]+"${pull_timeout[@]}"} docker pull --quiet "${BASE_IMAGE}" >/dev/null; then
    echo "::warning::docker pull timed out or failed; retrying once"
    ${pull_timeout[@]+"${pull_timeout[@]}"} docker pull --quiet "${BASE_IMAGE}" >/dev/null
  fi
  exec docker run --rm ${dns_args[@]+"${dns_args[@]}"} -v "${REPO_ROOT}:/src" -w /src -e IN_CONTAINER=1 \
    "${BASE_IMAGE}" bash scripts/compile_constraints.sh "$@"
fi

# --- inside the pinned target environment ---
# pip itself is pinned by the base-image digest (above); pin the compiler exactly so
# the resolved constraints only change when repo inputs change, not the toolchain.
# >=7.5.1 is required: older pip-tools with click>=8.3 writes bogus options
# (--cert=None, --no-index, --pip-args=None, ...) into the header command, which
# Renovate's pip-compile manager parses and rejects. Keep in sync with
# `constraints.pipTools` in .github/renovate.json5.
#
# Bound every network wait. Without these, a stalled index/CDN read inside pip's resolver
# (seen 2026-11-12: the asr lane went silent for 9+ minutes on four PRs, after prod resolved
# in ~18 s, while the asr lane normally takes about a minute) runs until the job is cancelled,
# with no output at all. Env vars rather than --pip-args, which pip-compile would write into
# the constraints header and so into the drift diff.
export PIP_DEFAULT_TIMEOUT="${PIP_DEFAULT_TIMEOUT:-20}" PIP_RETRIES="${PIP_RETRIES:-3}"
LANE_TIMEOUT="${LANE_TIMEOUT:-240}" # seconds per pip-compile attempt (healthy: 15-70 s)
timeouts_left=1                     # total timeout retries across all lanes, so the job stays bounded

python -m pip install --quiet "pip-tools==7.6.2"

compile_once() {
  local out="$1" log="$2"
  # Version-pinned (no --generate-hashes): the constraints are consumed via `pip -c`
  # alongside editable `pip install -e .`, and hash-checking mode is incompatible with
  # an unhashable editable install. Hash-verified `--require-hashes -r` for the immutable
  # images is a documented follow-up (see review/22 / constraints/README.md).
  # --verbose (not part of the header command) goes to a log that is printed on failure.
  timeout --kill-after=10 "${LANE_TIMEOUT}" \
    pip-compile --verbose --strip-extras --allow-unsafe \
    --output-file "constraints/${out}.txt" "constraints/${out}.in" >"${log}" 2>&1
}

compile() {
  local out="$1" log start rc
  log="$(mktemp)"
  echo "→ compiling constraints/${out}.txt"
  while :; do
    start=$SECONDS
    rc=0
    compile_once "${out}" "${log}" || rc=$?
    if [[ "${rc}" -eq 0 ]]; then
      echo "  ${out}: resolved in $((SECONDS - start))s"
      rm -f "${log}"
      return 0
    fi
    # pip-compile writes its output only on success, so a killed run leaves the committed
    # file untouched and the drift diff would pass. Every non-zero status therefore ends
    # the script with a failure: a skipped lane never reports success.
    echo "::group::pip-compile ${out} output (last 80 lines; exit ${rc} after $((SECONDS - start))s)"
    tail -n 80 "${log}"
    echo "::endgroup::"
    if [[ "${rc}" -eq 124 || "${rc}" -eq 137 ]]; then
      echo "::warning::pip-compile for ${out} timed out after ${LANE_TIMEOUT}s (network stall?)"
      if [[ "${timeouts_left}" -gt 0 ]]; then
        timeouts_left=$((timeouts_left - 1))
        echo "retrying ${out} once"
        continue
      fi
      echo "::error::constraints/${out}.txt not resolved: pip-compile timed out twice (a network stall, not drift; rerun the job)" >&2
    else
      # A real resolution failure (conflicting requirements, missing version): never retried.
      echo "::error::pip-compile failed for constraints/${out}.txt (exit ${rc})" >&2
    fi
    exit 1
  done
}

# One generated input per lane (constraints/<lane>.in, base deps + that lane's extras), so
# Renovate's pip-compile manager tracks each lock as its own project. The lane -> extras map
# lives in scripts/lane_inputs.py; keep constraints/README.md in sync with it.
python scripts/lane_inputs.py
compile prod
compile asr
compile chapter-research
compile dev

echo "Constraints recompiled. Review the diff before committing."
