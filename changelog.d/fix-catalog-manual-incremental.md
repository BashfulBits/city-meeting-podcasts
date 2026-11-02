- **Review/48 manual daily-incremental run.** The reconciliation workflow's manual dispatch gains
  an `incremental` input that runs exactly what the 08:20 UTC schedule runs (deferred checks, the
  stalest seventh of routes, availability signals and newly listed models), so a failed or
  skipped daily run can be repeated by hand. A manual run does not trigger the automatic
  retirement job, which keys on the scheduled run.
