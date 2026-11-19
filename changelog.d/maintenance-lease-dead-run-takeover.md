- Maintenance leases held by a GitHub Actions run that GitHub reports as completed are now taken
  over instead of blocking for the 6 h TTL. A runner that dies mid-run (twice on 2026-11-18, in the
  chapter-locator lane) never released its lease, so every later scheduled and dispatched run
  failed with `MaintenanceLeaseBusy`. Any uncertainty still fails closed.
