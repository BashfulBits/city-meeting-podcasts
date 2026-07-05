#!/usr/bin/env python3
"""Enforce the dependency-pinning policy (review/22) in CI.

Two static guards that need no network or compile step (the third guard — the
constraints-drift gate — runs ``pip-compile`` in the pinned container as a
separate ci.yml step):

1. Pinned-Actions guard: every third-party ``uses:`` in .github/workflows/*.yml
   must be pinned to a full 40-hex commit SHA (OpenSSF pinned-dependencies). Local
   actions (``./``) and ``docker://...@sha256:`` refs are allowed.
2. External-worker-dep guard: scripts/compute/{modal,beam}_app.py must NOT
   re-declare dependency versions (they install from the shared constraints).

Exit non-zero with a readable report on any violation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"

# `uses:` value capture; ignores commented lines.
USES_RE = re.compile(r"^\s*(?:-\s*)?uses:\s*['\"]?([^'\"\s#]+)['\"]?")
SHA_PINNED_RE = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def check_pinned_actions() -> list[str]:
    problems: list[str] = []
    for wf in sorted(WORKFLOWS.glob("*.yml")):
        for lineno, line in enumerate(wf.read_text().splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            m = USES_RE.match(line)
            if not m:
                continue
            ref = m.group(1)
            if ref.startswith("./") or ref.startswith("docker://") and "@sha256:" in ref:
                continue  # local action or digest-pinned container
            if not SHA_PINNED_RE.match(ref):
                rel = wf.relative_to(ROOT)
                problems.append(f"{rel}:{lineno}: not SHA-pinned -> uses: {ref}")
    return problems


# Package names that must live only in pyproject.toml + constraints, never re-declared
# in the external-worker image builders.
FORBIDDEN_IN_WORKERS = (
    "faster-whisper",
    "ctranslate2",
    "stable-ts",
    "boto3",
    "defusedxml",
    "Jinja2",
    "Pillow",
    "PyYAML",
    "requests",
)
WORKER_FILES = ("scripts/compute/modal_app.py", "scripts/compute/beam_app.py")


def check_external_worker_deps() -> list[str]:
    problems: list[str] = []
    for rel in WORKER_FILES:
        path = ROOT / rel
        if not path.exists():
            continue  # worker branches not merged yet
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for pkg in FORBIDDEN_IN_WORKERS:
                if f'"{pkg}' in line or f"'{pkg}" in line:
                    problems.append(
                        f"{rel}:{lineno}: hardcoded dependency '{pkg}' — install from "
                        f"constraints/asr.txt instead (see review/22)"
                    )
    return problems


def main() -> int:
    failures = 0
    for title, problems in (
        ("Unpinned GitHub Actions (must be @<40-hex commit SHA>)", check_pinned_actions()),
        ("Hardcoded deps in external-worker image builders", check_external_worker_deps()),
    ):
        if problems:
            failures += len(problems)
            print(f"\n✗ {title}:")
            for p in problems:
                print(f"  {p}")
    if failures:
        print(f"\nDependency-policy check failed: {failures} violation(s). See review/22.")
        return 1
    print("Dependency-policy check passed (pinned actions + external-worker deps).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
