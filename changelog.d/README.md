# Changelog Fragments (`changelog.d/`)

This directory holds unmerged changelog entries for active pull requests.

## Why use fragment files?

Editing `CHANGELOG.md` directly in concurrent pull requests causes git merge conflicts whenever
GitHub rebases or merges PRs onto `main`. When multiple PRs touch the same lines right under
`## Unreleased`, automated rebases and merge queues fail.

By writing a dedicated snippet file in this directory instead, each pull request introduces a unique
file path. Diffs never overlap, allowing GitHub to automatically rebase PRs without conflicts.

## Naming Convention

Name your fragment file after the issue, PR number, or branch slug:

```text
changelog.d/<id-or-slug>.md
```

Examples:
- `changelog.d/2284-structured-probe-diagnostics.md`
- `changelog.d/feat-input-calibration-canary.md`
- `changelog.d/fix-single-model-output-budget.md`

## Content Format

Each fragment must contain one or more Markdown entries following the
[Keep a Changelog](https://keepachangelog.com/) convention. Keep lines under 100 characters.

Subheadings like `### Added`, `### Fixed`, or `### Changed` are optional; the compiler
automatically normalizes them into clean list items under `## Unreleased`.

Example:
```markdown
- **Review/48 structured-probe diagnostics.** New-route JSON-mode checks retain bounded HTTP error
  and failed-completion previews, provider request IDs, stream and content sizes, usage counts,
  finish reasons, and diagnostic codes. API keys are redacted from bounded previews.
```

## How Fragments are Merged

When a pull request merges into `main`, the `compile-changelog.yml` GitHub Action runs
`scripts/compile_changelog.py` to:
1. Collect all `.md` snippets in `changelog.d/` (excluding this `README.md`).
2. Insert them in order under the `## Unreleased` section of `CHANGELOG.md`.
3. Delete the fragment files from `changelog.d/`.
4. Commit the compiled `CHANGELOG.md` directly to `main`.
