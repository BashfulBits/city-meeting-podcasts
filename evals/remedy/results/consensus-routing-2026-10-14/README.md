# Consensus-routing evaluation record

This package documents the evidence behind the P3 workflow decision in
[`review/51`](../../../../review/51-unexpected-body-remedy-flow.md#p3--two-stage-consensus-and-bounded-adjudication).
It contains the frozen 60-recording comparison, the eight disagreement packets, raw observations,
scoring scripts and final local reports. The run was diagnostic on reused cases; it is not route
admission or production qualification.

## What was evaluated

The prompt comparison used the same 60 recordings with Sol, GLM Flash and DeepSeek V4.1 Flash for
each of three general prompt recipes: `original`, `evidence_first` and `self_check` (540 completed
answers). On the original prompt, Sol + GLM Flash agreed correctly on 52 cases, had no shared wrong
agreements, and disagreed on eight. The remaining eight were sent to one broader-context follow-up
with DeepSeek V4.1 Flash and JEV. DeepSeek produced a full assignment. JEV used the existing local
pilot because its Worker B2 payload upload failed before enqueue; JEV returned a bounded A/B/neither/
insufficient-evidence choice and support probabilities. That pilot result does not qualify JEV's
production dispatch route.

The two result directories retain each study separately:

- `../prompt-comparison-2026-10-14/` — comparison scripts, frozen report, intents and batches.
- `../disagreement-2026-10-14/` — eight-case packet, preparation and run scripts, observations,
  scoring script and final adjudication report.
- `../../holdout/body-selection-2026-10-13/` — the shared frozen manifest and gold used for
  offline scoring. Gold was not included in model inputs.
- `../disagreement-2026-10-14/human-dispositions-2026-10-15.json` — the maintainer's separate
  feed-by-feed labels; it does not revise the frozen gold. Unlisted feeds are unrated.

## Offline scoring

Run these from the repository root. They read saved observations and frozen gold only; they do not
call models. Use a new output path because reports are immutable.

```bash
PYTHONPATH=. python evals/remedy/results/prompt-comparison-2026-10-14/score.py \
  --gold evals/remedy/holdout/body-selection-2026-10-13/gold.json \
  --out /tmp/prompt-comparison-rescore.json

PYTHONPATH=. python evals/remedy/results/disagreement-2026-10-14/score.py \
  --gold evals/remedy/holdout/body-selection-2026-10-13/gold.json \
  --out /tmp/disagreement-rescore.json
```

The committed final reports are `../prompt-comparison-2026-10-14/scored-2026-10-14-final-v1.json`
and `../disagreement-2026-10-14/scored-2026-10-15-final-v1.json`. The prompt-comparison run's
saved binding matches normal coordination mode; do not pass `--coordination-bypass` when scoring
these observations.

## Live execution boundary

`prompt-comparison-2026-10-14/run.py` without `--execute` prints a local plan. `--execute` can
submit provider requests. The completed run must not be restarted or duplicated. Any future live
evaluation needs fresh maintainer authorization, approved route admissions, shared quota and
production coordination, and a new immutable run binding and output directory. The disagreement
`run.py` and `local_jev.py` also submit calls; use them only for a newly approved experiment after
reviewing the route and transport gates in `review/51`.

## Interpretation limits

Pair agreement counts measure agreement with frozen truth for this reused set; they do not replace
feed-level extra-feed/omission errors or the five unknown-owner guardrails. JEV's bounded choice is
not full assignment accuracy. All reports retain `qualified: false`; no result here admits a model,
enables production routing, creates a feed, or authorizes a feed change without human review.
