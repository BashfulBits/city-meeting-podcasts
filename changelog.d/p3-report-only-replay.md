- **P3 report-only replay.** Added a strict local replay tool for recorded unexpected-body votes.
  It emits a new decision report and fails closed with the repository's empty route admissions;
  it makes no model calls or GitHub, storage, feed, or production-routing changes. P3 dispatch
  integration remains gated separately.
- **P3 dispatch cadence.** Recorded the maintainer's decision for a dedicated daily Worker caller,
  a shared adjudicator interface with the existing JEV wire adapter, and a physical BeatAPI route
  constraint for DeepSeek. The scheduled caller remains gated on durable P2 state and admission.
- **Worker route pinning.** Queue-only dispatch can now narrow a logical model pool to explicit
  physical route IDs. Pins cannot widen the model policy, and unavailable pinned routes do not
  fall back to another provider.
