### Changed
- GLM 5.3 Flash on OrcaRouter now runs `judge:adjudicator` at `reasoning_effort: low`. On a
  10-request sample it cut reasoning from a median 27.6k to 1.7k tokens and the call from 411 s
  to 37 s, with 87.5% verdict agreement against the default (production against the default:
  88%). It leans lenient: 16 flips to "supported" against 4 the other way (#2689).
- `chapter-agenda` now runs Nemotron 3 Ultra with reasoning off on all three legs (NVIDIA
  `enable_thinking: false`; Kilo and OpenRouter, which now declare `reasoning_controls.off`,
  `reasoning_effort: "none"`). On the golden set (main + holdout) at 780 s: F1 0.749 / 0.825,
  valid 100% / 100%, 0 of 56 calls timed out, p50 8 s, against reasoning on 0.750 / 0.840 and
  11 of 21 production NVIDIA calls timing out. Not a re-key: reasoning is not in the recipe hash.
  Nemotron 3.5 Super VL (default reasoning) scored 0.708 / 0.860, p50 50 s, and was not adopted.

### Documentation
- review/58 section 12.5, the throughput evidence file and the `evals/chapter-agenda` README
  record the GLM, Ultra and Kilo and OpenRouter results and the golden agenda eval. Results in
  `evals/chapter-agenda/results/2026-11-15-nemotron-reasoning*.json`.
