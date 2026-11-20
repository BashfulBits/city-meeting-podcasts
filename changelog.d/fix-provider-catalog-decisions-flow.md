- **Provider catalog decisions: add / ignore / remove.** The rolling issue now shows each new model
  as one quality line (AA Index vs. our lanes' range, lanes it clears, size, context, free-tier end)
  and one line per provider route (ready/testing/needs you, tested caps), with only `add`/`ignore`
  (or `remove`/`ignore` for paid routes). Lane-assignment, `test again` and `AA match` rows are gone;
  old ticks carry over. The body drops from ~65k to ~10k readable characters.
- **`/apply` opens the PR.** Additions no longer defer on new identities, lane changes or contended
  proofs; routes inherit backup tier from backup-only providers; additions-only applies skip the
  per-provider drain (previously up to 13 minutes each). A pinned BeatAPI route test that failed
  every catalog addition now checks invariants instead.
