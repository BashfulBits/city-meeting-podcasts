#!/usr/bin/env bash
# Pre-push hook / verification script for city-meeting-podcasts.
# Run automatically before `git push` (if core.hooksPath is set to .githooks) or directly via:
#   ./scripts/pre-push.sh

set -euo pipefail

echo "==> Running ruff linter check..."
ruff check .

echo "==> Running ruff format check..."
ruff format --check .

echo "==> Compiling & checking LLM limits..."
python scripts/compile_llm_limits.py
git diff --exit-code workers/llm-dispatch-v2/src/dispatch_limits.json workers/llm-dispatch-proxy/src/dispatch_limits.json citypods/compute/llm_routes.json

echo "==> Running offline test suite..."
pytest -q

echo "==> All pre-push checks passed successfully."

