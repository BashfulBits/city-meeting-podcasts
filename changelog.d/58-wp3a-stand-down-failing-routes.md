- **Review/58 WP3a: stand down routes that cannot succeed.** `beatapi_jev_1_13_free`
  (`judge:anchor`: 717 HTTP 400 against 26 successes, 0 of 882 jobs recovered) and both BeatAPI GLM 5.3 Flash routes
  (`judge:adjudicator`: 721 HTTP 524 at BeatAPI's ~125 s edge limit) are paused with the existing
  `rpd: 0` convention. The primary-account BeatAPI chat routes no longer `yield_to` the paused
  JEV route. Queued `typesafe/jev-1.13` jobs are held, not failed, until review/58 WP3c restores
  the route. Stale dispatch-tuning comments corrected (comments only, no values changed). Deploy
  the recompiled `dispatch_limits.json` Worker to take effect; no pipeline-version bump and no
  stored artifact is invalidated.
