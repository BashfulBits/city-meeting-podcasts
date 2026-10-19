- **The tag tournament and R5 benchmark are retired (review/53 PR11a).** `citypods/tournament.py`,
  `tournament_backfill.py`, `r5_benchmark.py`, `scripts/tournament_route_proposal.py`, their five
  workflows (`llm-tournament`, `r5-benchmark`, `tournament-tag-backfill`,
  `tournament-ticket-approve`, `tournament-route-merged`), the `tournament` config block, and the
  `tournament:tag`, `tournament:tag-judge`, `r5-benchmark:tag` and `r5-benchmark:judge` lanes are
  removed; `ingress_reservations.json` is now 10 lanes. The pre-labeler and R5 evaluation matrix
  are PR11b.
