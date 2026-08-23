from __future__ import annotations

from scripts.check_dependency_policy import (
    check_external_worker_deps,
    check_pinned_actions,
    check_renovate_coverage,
)


def test_pinned_actions_guard_passes():
    problems = check_pinned_actions()
    assert problems == [], f"Found unpinned actions: {problems}"


def test_external_worker_deps_guard_passes():
    problems = check_external_worker_deps()
    assert problems == [], f"Found hardcoded worker deps: {problems}"


def test_renovate_coverage_guard_passes():
    problems = check_renovate_coverage()
    assert problems == [], f"Found unclassified dependencies in renovate.json5: {problems}"
