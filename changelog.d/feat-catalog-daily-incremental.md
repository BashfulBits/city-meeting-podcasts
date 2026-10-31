- **Review/48 daily incremental reconcile.** The daily run (08:20 UTC) now also health-checks the
  least recently checked seventh of the configured routes (`--incremental`, in addition to
  quota-deferred checks), so every route is checked within a week and a dead or unentitled one is
  found within days instead of at the weekly full run. Automatic route-removal PRs now follow the
  daily run as well as the weekly one, and re-probe only the routes the rolling issue flags as
  `retired`/`not_served` rather than every route. The weekly full run remains the backstop for new
  candidates, canaries and discovery. The daily run is renamed `daily-incremental`.
