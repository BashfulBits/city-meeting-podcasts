### Added

- Perplexity Decider V1.3 wired as a second, non-Google `judge:anchor` for Google-produced
  entries: Worker API shape `openrouter_decisions` (`POST /api/alpha/decisions`), provider
  `openrouter_decisions` via the `custom-openrouter-api` AI Gateway provider, a free route held at
  `rpd: 0`, `DeciderBackend`, family `qwen`. Inactive (eligible only) until the owner registers the
  gateway provider (review/58 12.9).

### Changed

- The judge anchor is chosen per entry: the first anchor model independent of the entry's
  producers. Siblings and adjudicators exclude that entry's own anchor family.
- Rule subjects no longer balance into the `secondary` sibling slot, which is kept for the
  Nemotron-produced entries only Qwen3.8 may judge.
