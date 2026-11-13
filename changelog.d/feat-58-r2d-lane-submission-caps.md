- **Lane submission caps re-derived (review/58 section 12.4).** The `llm_lanes` daily write
  units, reservations and queue caps are now sized by one rule from measured demand, route supply
  and DO rows, instead of an 800-meetings planning case. Every cap filled at once would cost about
  71,550 rows (84% of the 85,000 pace); the old caps would have cost about 195,000.
  - Jobs/day: agenda 300, locator 1,000, tagger 500, moments 200, judge anchor 400, sibling 900,
    adjudicator 200; remedy is unchanged.
  - `MAX_JOBS_PER_UTC_DAY`, the row stops and the judge outstanding-packet limits are unchanged.
  - Three adjacent problems are filed as #2671, #2672 and #2673.
