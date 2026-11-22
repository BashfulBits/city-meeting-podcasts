- **Provider catalog history survives a transient network error.** `gh api` reads of workflow
  runs and evidence artifacts retry up to three times (after 5 s and 20 s) on connection resets,
  timeouts and 5xx responses; other errors still fail at once. The 2026-11-22 weekly run lost its
  whole configured-route re-measure to one "connection reset by peer" on an old artifact. The skip
  message now says why the history was unavailable.
