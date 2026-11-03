### Fixed

- **An accepted AA match no longer keeps a dropped candidate listed.** Ticking an AA match for a
  model that the match then re-scored below the quality bar left it on the provider-catalog issue
  as a "pending prior selection" indefinitely. The match is a note for the next run, not a
  decision to fulfil, so it no longer counts as one.
