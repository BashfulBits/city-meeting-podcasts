### Added
- Provider catalog: an unscored candidate now lists the three closest Artificial Analysis models as checkboxes under **Decisions**; ticking one makes the next daily run score the model from that entry (no PR needed, kept in the issue state). AA names and separator spellings (`m3.1` / `m3-1`) are matched automatically when exactly one model answers to them, with the Hugging Face repo name as a tiebreaker.
- Provider catalog: each candidate the daily run has not measured now says why (waited behind others, time spent, catalog failed, not planned).

### Changed
- Provider catalog: a 429 with no `Retry-After` during the size search no longer ends the candidate for the day. The session is released, the provider's other candidates get their turn, and the search resumes from its saved bounds after 30 s, doubling to 10 minutes; a 429 after the 10-minute wait gives up until the next run.
- Provider catalog: the daily run serves the candidates with the fewest tries first and visits every planned candidate per provider; a repeated provider-stated context-limit refusal now aims 5% lower each time (floor 80%). Carried candidates are re-scored against current AA data each run.
