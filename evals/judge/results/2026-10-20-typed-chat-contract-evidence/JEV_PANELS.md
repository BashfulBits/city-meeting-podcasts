# Agreed JEV panels: matched-candidate evaluation

Both agreed panels retain useful positive coverage with zero false accepts on the 53 definite
labels at the descriptive JEV `p >= 0.5` cutoff. Keep JEV + Gemma / JEV + Nemotron, selected by
producer-family independence. Every result without two positive votes goes to human review.
This report changes no production threshold, rejection behavior, model assignment, or gold label.

## Inputs and compatibility

The September 30 JEV bundling results (question layout, both orders) and context-ladder prompt-2
T1 results join to the October typed-v3 chat results by frozen candidate ID. The manifest at
JEV context run commit `f7a647db` equals today's manifest, including all evidence strings. All
53 definite labels have a JEV probability; two uncertain labels remain excluded. There are
18 positives and 35 negatives: 30 synthetic examples, ten planted controls, and 13 real examples
with Claude adjudication and maintainer corrections. This is a development selection set.
The bundling run records pre-manifest commit `fa522ec9`; it has matching IDs/set version, but
no saved request bodies to independently hash-verify its original synthetic prompts.

The requests are not identical across backends: historical JEV tag questions include chapter
titles and the maintainer rubric; chat requests use the proposed tag and T1 evidence. JEV saved
probabilities were joined, not replayed through today's JEV parser; raw JEV envelopes are absent.
Chat first-200 replies were re-parsed with today's production parser and strict schema. Existing
labeled decisions reproduced unchanged. Compact observations now retain per-answer quote checks,
so adjudicator scoring can also be replayed without committing raw replies.

## Results at the reference cutoff

| Panel | Correct positive accepts / 18 | False accepts / 35 | Human review / 53 |
|---|---:|---:|---:|
| JEV + NVIDIA Gemma 4 31B IT | 16 | 0 | 37 |
| JEV + OpenRouter Nemotron 3.5 Super VL (configured sibling identity) | 16 | 0 | 37 |
| JEV + native NVIDIA Nemotron (route sensitivity) | 16 | 0 | 37 |
| JEV alone (comparison only) | 16 | 1 | 36 |

Both panels capture 88.9% of labeled positives and auto-accept 30.2% of this deliberately
negative-heavy set. The remaining cases include all 35 negatives and two positives (`i27`, `i28`).
Human review is a routing outcome, not a negative model prediction. Zero observed false accepts
among 35 negatives has a Wilson 95% interval of 0–9.9%; this is not a production safety guarantee.
Both synthetic question orders produce identical scoring at every swept cutoff.

JEV alone incorrectly accepts `i16` (`p=0.55`); both siblings reject it. Conversely, JEV rejects
`i2` (`p=0.43`), which all chat models accept against the unchanged negative label. The conjunction
eliminates both observed errors. `i2` remains flagged for maintainer label review; it was not
changed or excluded to improve the result.

Raising the cutoff to 0.6 or 0.7 leaves both panel counts unchanged. Cutoffs 0.8, 0.9 and 0.95
retain respectively 15, 13 and 11 correct accepts, with zero false accepts in this set. There is
no observed pair benefit from raising the cutoff above 0.5 here. This sensitivity sweep supports
retaining 0.5 as an evaluation reference; numerical production policy still needs its own reviewed
calibration/fingerprint change. No moment gate/grade/choose accuracy is claimed: those lack gold.

## Independent adjudicators on binary disagreements

Each adjudicator is a separate counterfactual, not an extra two-model vote. The subset uses
JEV `p >= 0.5` versus the sibling boolean, independently of the admission sensitivity cutoff.
It excludes missing/invalid sibling answers. It does not reproduce the production escalation-band,
random calibration sampling, highest-tier selection, or full-context adjudication requests.
Producer-family metadata must also be checked when applying these models to actual entries;
these counts do not certify eligibility for every historical producer.

| Panel's disputed subset | Disputed | NVIDIA DeepSeek usable / correct | GLM usable / correct |
|---|---:|---:|---:|
| JEV + NVIDIA Gemma | 4 | 4 / 3 | 3 / 2 |
| JEV + OpenRouter Nemotron | 4 | 4 / 3 | 3 / 2 |
| JEV + native NVIDIA Nemotron | 3 | 3 / 2 | 2 / 1 |

Adjudicator answers require both a passing packet contract and a valid quote for that answer.
GLM cannot contribute `i16`: its packet fails the strict contract. NVIDIA DeepSeek correctly
rejects `i16`; both adjudicators correctly support positive `i28`. Both incorrectly support
negative `i2`. Native Gemma's extra disagreement is negative `i0`, correctly rejected by both.
OpenRouter Nemotron's extra disagreement is positive `i27`, correctly supported by both.

Keep the selected GLM or NVIDIA DeepSeek as independent evidence for review; these tiny subsets
do not establish a preferred adjudicator's accuracy. Automatically accepting positive adjudicator
answers on disputes would reintroduce the observed `i2` false accept. The accepted workflow here
continues to send non-consensus outcomes to human review.

## Replay

From the repository root, using committed compact observations:

```bash
PYTHONPATH="$PWD" python scripts/score_jev_panels.py \
  --observations evals/judge/results/2026-10-20-typed-chat-contract-evidence/observations.json \
  --jev-bundling evals/judge/results/2026-09-30-bundling-jev.json \
  --jev-context evals/judge/results/2026-09-30-context-ladder-jev-p2.json \
  --out /tmp/new-jev-panel-report.json
```

Use a fresh output path. Replace `--observations` with `--archive /path/to/raw.tar.gz` to re-parse
original chat first-200 replies from the externally retained archive. No model calls occur.
The generated JSON records source hashes, historical JEV metadata, all route sensitivities,
contested IDs and quote-gated adjudicator decisions. Sibling quote quality remains non-gating.

Next: use these retained panels for a disjoint labeled validation set before claiming a numerical
production false-accept bound. Population-weighted PR7 calibration is separate from this completed
matched-candidate analysis; lack of clean catalog pairs did not block this comparison.
