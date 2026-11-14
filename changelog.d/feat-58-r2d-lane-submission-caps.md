- **Lane submission caps re-derived (review/58 section 12.4).** Each `llm_lanes` entry now carries
  two numbers with separate jobs. Both replace the 800-meetings planning case.
  - `reserved_write_units` is the steady-state share: jobs per new meeting × 14 meetings a day
    (p90) × a priority headroom (feeders ×4, tagger and moments ×3, judge lanes ×2). The lanes
    together reserve 670 units, down from 16,116.
  - `daily_write_units` / `max_queued_jobs` is the ceiling: the reservation plus a drain allowance
    for the lane's measured backlog (at least 100 jobs a day), bounded by route supply.
  - Ceilings in jobs/day: agenda 110, locator 110, tagger 650, moments 110, judge anchor 120,
    sibling 130, adjudicator 110; remedy is unchanged.
  - Every ceiling filled at once costs about 32,600 rows. That is under every global guard, so each
    lane gets its whole ceiling. `MAX_JOBS_PER_UTC_DAY`, the row stops and the judge
    outstanding-packet limits are unchanged.
  - Three adjacent problems are filed as #2671, #2672 and #2673.
