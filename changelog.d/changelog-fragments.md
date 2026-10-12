- **Changelog fragments and automated compiler workflow.** PRs now add isolated fragment
  files in `changelog.d/<slug>.md` instead of editing `CHANGELOG.md` directly, eliminating
  merge and rebase conflicts on GitHub when multiple PRs land in sequence. A GitHub Action
  compiles fragments into `CHANGELOG.md` under `## Unreleased` and removes them upon merge
  to `main`.
