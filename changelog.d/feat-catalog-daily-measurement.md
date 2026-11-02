- **Review/48 measures found candidates on the daily run; the quality bar no longer depends on
  named models.** The bar a candidate must clear (for automatic measurement and the "below floor"
  flag) is now the weakest Artificial Analysis-scored model our lanes actually run, recomputed
  from AA's current scores every run, instead of two pinned models AA stopped scoring; the pinned
  pair is only a fallback. The daily incremental run (scheduled, or repeated by hand) now runs one
  size-search session per day on an already-found candidate: an input search first, then an
  output search for a candidate with a measured input and no published output limit; unscored
  models are measured too (a scored model at or below the bar is skipped). `/apply` takes the
  route's output limit from a verified output search, a provider claim or LiteLLM, and defers
  until one exists. The provisional input/output caps for route-only adds are removed: every
  added route now carries measured limits. The daily job timeout is 180 minutes.
