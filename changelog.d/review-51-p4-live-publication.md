- **Remedy publication, live stage (review/51 P4).** With `publication_mode: live` in
  `config/remedy.yml` (default `report_only`), the P3 publisher executes its plan: one isolated
  worktree and PR per unit of at most five decisions, manifest-limited staging, the repository
  gate on the exact tree, and stable decision markers and bindings in every body.
  `remedy-feedback.yml` turns a human merge, close or reopen into a P2 decision event (a
  rejected PR gets one refinement issue and is never regenerated on the same evidence), a daily
  reconciliation recovers missed events, and `/remedy reopen <decision_id>` lets a maintainer
  ask for reconsideration. The switch is excluded from the config fingerprint, so flipping it
  re-asks no model.
