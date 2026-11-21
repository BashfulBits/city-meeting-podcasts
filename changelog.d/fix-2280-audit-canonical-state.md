### Fixed

- Feed-health audit (#2280): `audit.yml` now configures the R2 routing backend. Without it the audit
  restored a stale 425-object B2 manifest with no `sources/` entries, so it ran against empty
  records and reported false `unexpected-body` findings (#1623) and wrong backlog inferences. A
  test keeps every workflow that reads canonical state configured for R2.
