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
  exec docker run --rm -v "${REPO_ROOT}:/src" -w /src -e IN_CONTAINER=1 \
    "${BASE_IMAGE}" bash scripts/compile_constraints.sh "$@"
fi

# --- inside the pinned target environment ---
python -m pip install --quiet --upgrade pip
python -m pip install --quiet "pip-tools>=7.4"

compile() {
  local out="$1"; shift
  echo "→ compiling constraints/${out}"
  pip-compile --quiet --strip-extras --generate-hashes --allow-unsafe \
    --output-file "constraints/${out}" "$@" pyproject.toml
}

# Profiles mirror the extras in pyproject.toml; keep in sync with constraints/README.md.
compile prod.txt --extra storage
compile asr.txt  --extra storage --extra asr
compile dev.txt  --extra storage --extra dev

echo "Constraints recompiled. Review the diff before committing."
