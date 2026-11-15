### Changed
- GLM 5.3 Flash on OrcaRouter now runs `judge:adjudicator` at `reasoning_effort: low`. On a
  10-request sample it cut reasoning from a median 27.6k to 1.7k tokens and the call from 411 s
  to 37 s, with 87.5% verdict agreement against the default (production against the default:
  88%). It leans lenient: 16 flips to "supported" against 4 the other way (#2689).
- The Kilo and OpenRouter Nemotron 3 Ultra routes declare `reasoning_controls.off`
  (`reasoning_effort: "none"`; 0 reasoning tokens on 2 of 2 calls each). No lane uses it.

### Documentation
- review/58 section 12.5 and the throughput evidence file record the GLM, Ultra and Kilo and
  OpenRouter results, with the Ultra recall numbers (mean line recall 0.88) for the owner's
  decision. No lane turns Ultra reasoning off.
- Golden `chapter-agenda` eval (main + holdout) of Nemotron 3 Ultra with reasoning off and
  Nemotron 3.5 Super VL with default reasoning, at 780 s: Ultra off F1 0.749 / 0.825, 0 of 56
  calls timed out, p50 8 s (reasoning on: 0.750 / 0.840); Super F1 0.708 / 0.860, 0 of 57, p50
  50 s. Results in `evals/chapter-agenda/results/2026-11-15-nemotron-reasoning*.json`.
