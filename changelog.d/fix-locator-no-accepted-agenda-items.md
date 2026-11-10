- **Chapter locator no longer errors on agendas with no accepted items.** A completed agenda
  artifact whose items were all rejected used to build a locator job and raise "locator requires at
  least one agenda item" every run. The stage now defers it as `no-accepted-agenda-items` (mapped
  to `blocked` in the backlog trend) before reading the transcript. The chapter-locator workflow
  runner is pinned to `ubuntu-24.04`.
- **Locator duplicate-unit errors name both agenda items.** "duplicate locator unit: uNNNNN" now
  also reports the two agenda item indexes, so a repeated agenda entry can be told apart from the
  model anchoring distinct items to one unit.
