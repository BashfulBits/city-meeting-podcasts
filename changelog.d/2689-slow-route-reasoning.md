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
