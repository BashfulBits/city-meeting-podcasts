#!/usr/bin/env bash
# Pre-push hook / verification script for city-meeting-podcasts.
# Run automatically before `git push` (if core.hooksPath is set to .githooks) or directly via:
#   ./scripts/pre-push.sh

set -euo pipefail

echo "==> Running ruff linter check..."
ruff check .

echo "==> Running ruff format check..."
ruff format --check .

echo "==> Running offline test suite..."
pytest -q

echo "==> All pre-push checks passed successfully."
