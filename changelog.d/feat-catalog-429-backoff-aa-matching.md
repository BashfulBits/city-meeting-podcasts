### Added
- Provider catalog: an unscored candidate lists the three closest Artificial Analysis models as
  checkboxes under **Decisions**. Ticking one makes the next daily run score the model from that
  entry (no PR needed). AA names and separator spellings (`m3.1` / `m3-1`) also match
  automatically when exactly one model answers to them.
- Provider catalog: each candidate the daily run has not measured says why.

### Changed
- Provider catalog: a 429 with no `Retry-After` during the size search no longer ends the
  candidate for the day. The provider's other candidates run first, then the search resumes from
  its saved bounds after 30 s, doubling to 10 minutes; a 429 after the 10-minute wait gives up
  until the next run.
- Provider catalog: the daily run serves the candidates with the fewest tries first and visits
  every planned candidate per provider. A repeated provider-stated context-limit refusal aims 5%
  lower each time (floor 80%). Carried candidates are re-scored against current AA data.
