# open-weight decision evaluation set

Frozen inputs and truth for [`review/60`](../../review/60-open-weight-decision-model-evaluation.md) (Phase 2). It
scores any decision model (Perplexity, Gemini decision, Karpo, GLiDE, ...) on the same items.

| File | Role |
|---|---|
| `manifest.json` | frozen **inputs** only: no labels, verdicts or suggestions |
| `gold.json` | the **truth**, with the source of each label |
| `production_reference.json` | production judge verdicts, the production outcome stratum of each item, and rule/LLM tag suggestions, for agreement and weighting only |
| `freeze_from_sample.py` | how the files were derived from the private labeling sample |

## Lanes

- **tags** (240): is the proposed tag supported by its chapter evidence (the production T1 and T2 texts)? Labels are
  `supported`, `not_supported`, `uncertain` and come from the maintainer, **blind** (production verdicts hidden). The
  items are 80 each of production outcome `yes`, `no` and `split` (the stratum lives in `production_reference.json`).
- **moments** (60): does the quote, as spoken, say what the stated reason claims? 26 items keep the production passage and
  the maintainer's label. 34 items had a passage that did not contain the quote (the 400-word cap defect in
  review/60); their evidence was rebuilt around the quote and labeled by **Claude**, not a human
  (`evidence_source: rebuilt-quote-centered`, `source: claude`). 58 of 60 are `supported`, so this lane cannot separate
  judges; it detects wrongly rejected quotes.
- **chapters** (60): lane B tagging. Each chapter shows 12 tags (suggestions plus similar and random tags). `gold.json`
  holds the tags **Claude** chose; they are a model-made reference, not human truth, and have not been spot-checked.
  Score on the 12 shown tags only. Cost and speed are measured with all 38 taxonomy tags (review/60 Phase 0).

## Split

`dev` or `holdout` by a stable SHA-256 of the episode (`src`, `uid`), so no episode's evidence is on both sides (about 50/50 by items). Tune on `dev`; leave `holdout` untouched until a
final run. Exclude `uncertain` from accuracy. Weight tag strata (joined by id from `production_reference.json`) back to the production mix for a single number,
and report paired bootstrap intervals; with this many items small differences are not significant.

## Coverage of existing gold

`evals/judge/gold.json` has 30 synthetic bundling items and 10 planted controls; `evals/judge/tag/gold.json` and
`evals/judge/moment/gold.json` are empty. This set is the first real human-labeled tag gold. Gaps: moments have only
`supported` (publishable and usefulness questions are not covered), chapter truth is not human, and 240 tag items are
enough for large effects only. More human labels are needed for the chapter lane and the moment negatives.
