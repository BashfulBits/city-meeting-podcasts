# judge evaluation lane

The pilot lane for the judge stack in [review/49](../../review/49-judge-consensus-admission.md): JEV as the
anchor judge, a sibling LLM, and an adjudicator decide whether a producer's output is valid or which of several is
best. It answers the design questions that cannot be settled by reading, and it is meant to be **re-run** whenever a
judge, prompt, context rule or task changes. It follows the layout of [`evals/chapter-agenda`](../chapter-agenda/README.md):
frozen inputs, separate truth, dated results, one script.

| File | Role |
|---|---|
| `manifest.json` | frozen **inputs** only (no answers), versioned |
| `gold.json` | the **truth** the inputs are scored against |
| `results/<date>-<experiment>-<judge>.json` | one run each; always records the set version, prompt version and git sha |
| `../../scripts/eval_judge.py` | `freeze`, `run`, `report` |
| `../../tests/test_eval_judge.py` | mocked tests (no network) for sizing, clients, builders, metrics, freeze and runs |

## Experiments

| Experiment | Judge | Question | Truth |
|---|---|---|---|
| `bundling` | JEV | does putting each item's evidence **in its own question** (tiny state) work as well as putting all evidence **in the shared state**? two layouts, two shuffled orders | synthetic items with known truth: 10 supported, 10 near-miss (same subject, tabled or denied), 10 off-topic |
| `question-types` | JEV and Qwen | how do `validate` (noul), `grade` (score) and `choose` (choice, best of four) behave on real pull quotes from real meetings; do judges agree | none exists for quote quality, so it measures **consistency**: order stability, agreement between question kinds and between judges |
| `context-ladder` | JEV | how much context should a judge see for a tag candidate? three tiers: **T0** the matched span, **T1** about 90 seconds around it (400 words), **T2** the whole chapter (1,800 words) | 10 planted wrong-tag controls (a real excerpt paired with a tag from an unrelated taxonomy group) must be rejected; real rule matches are unverified, so separation from controls and agreement between tiers are reported, not accuracy |
| `adjudicator` | Qwen | can a long-context model with thinking on settle supported/not-supported in packs that fit its 6k-token input limit | the same synthetic items |

## Frozen set (version 1)

Frozen from the durable state on 2026-09-30: 30 synthetic bundling items (seeded), 6 real council meetings with 4 pull
quotes each (seeded sample from 390 real shadow candidates), 34 real chapter-scoped rule candidates (at most 2 per tag,
seeded) plus 10 controls, with transcript excerpts taken from the public VTT files and capped (120, 400 and 1,800 words;
two real transcript cues were single paragraphs of thousands of words, which is why the caps exist). Re-freezing is a
reviewed change that bumps `SET_VERSION` and is run with `freeze --state-dir <pulled state> --force`; results always record the
version they used. Known limits: the bundling items are easy and synthetic; the question-type set is small and has no quality
truth; the context-ladder real items have no human labels (the controls are the only truth), so **accuracy of a tier against
the truth of real items is not measured here**; the continuous measurement in review/49 section 4b (a stratified sample judged at every
tier and by the adjudicator) is what fills that gap.

## Limits enforced in code

These were each learned the hard way while building the lane; the harness refuses to repeat them.

- **JEV** takes about 64k total input tokens **and** about 32k for the state plus the single largest question; the script keeps
  requests under 58k and 28k using a deliberately conservative estimate (1.35 tokens per word). An oversized request is answered with a
  misleading `503 processing_failed, retryable: true`, so a 503 near a ceiling is recorded as `oversize_suspected` and **never retried**.
  One successful call per minute on the free tier: calls are paced at 66 seconds.
- **Groq Qwen** caps a request near 7k input tokens (the script keeps prompts under 6k) and **1,000 output tokens per minute**, which
  reasoning-mode answers of 450 to 730 tokens hit after two requests: calls are spaced from their measured completion tokens.
- **Keys** are stripped of whitespace (a trailing carriage return once made a valid key read as invalid).
- A run with more than 10% unanswered calls is **inconclusive**, not a result (exit code 2), and errors are recorded, never hidden.

## Decision rules these results feed

- **Bundling:** use evidence-in-the-question for small, non-overlapping evidence and the shared state for shared or overlapping
  evidence; both are valid if they agree with the truth and with each other.
- **Context ladder (review/49 section 4b):** choose the smallest tier whose agreement with the adjudicator is not significantly worse than
  the next tier up, and escalate a contested item to the next tier before it reaches the adjudicator. The script reports the speed side
  of that trade: `items_per_call_at_ceiling` and `projected_calls_for_backfill` (for the 127,719 rule candidates in storage on 2026-09-30).
- **Question types:** a kind is usable as a gate if it is stable under reordering and agrees across judges; `grade` is a tie-break when its
  confidence is low.

## How to run

```bash
citypods-env python scripts/eval_judge.py run bundling --judge jev
citypods-env python scripts/eval_judge.py run question-types --judge jev
citypods-env python scripts/eval_judge.py run question-types --judge qwen
citypods-env python scripts/eval_judge.py run context-ladder --judge jev
citypods-env python scripts/eval_judge.py run adjudicator --judge qwen
python scripts/eval_judge.py run context-ladder --judge jev --dry-run     # plan and projected cost only
python scripts/eval_judge.py report evals/judge/results/*.json
```

`BEATAPI_API_KEY` and `GROQ_API_KEY` come from the environment. A full JEV pass (bundling, question types, context ladder) is about
a dozen calls and takes roughly fifteen minutes because of the one-a-minute pacing; the free tiers make every experiment here cost nothing.
Adding a task to the framework (a new question kind or evidence builder) means adding an experiment here first, so the lane grows
with the judge stack instead of being replaced by ad hoc scripts.

## Results (2026-09-30, set version 1, git `fa522ec9`; raw files in `results/`)

**bundling (JEV).** Evidence carried in each question (5.2k input tokens) and evidence in the shared state (5.4k): both
scored accuracy 1.00 and AUC 1.000, with no verdict flipping between option orders or between layouts (largest probability change 0.01
within a layout, 0.05 between them). The in-question layout separated slightly more cleanly (largest false-item score 0.02 against 0.06 to 0.07
in the state layout). Reading: JEV answers from evidence in the question text as well as from the state. The items are easy and synthetic, so
this shows both layouts are valid and stable, not that they are equally accurate on hard text.

**question-types (JEV and Qwen).** On six real meetings (four quotes each): the `choose` winner was the **same under both option orders in 6 of 6**
meetings; publish probability moved at most 0.06 and grade at most 0.16 between orders; publish probability ranged 0.40 to 0.835. The question kinds
do **not** pick the same quote: the `choose` winner equalled the top `grade` in 2 of 6 and the top publish probability in 2 of 6, and Qwen picked the same
winner as JEV in **3 of 6** (both orders). The producer's own score matched the JEV winner in 4 of 6. An exploratory run on a different six meetings
(not committed) gave Qwen-JEV agreement of 5 of 6 and 6 of 6 and producer-winner agreement of 1 of 6, so agreement varies a lot with the sample at this size:
treat the kinds as measuring different things, not as interchangeable, and size the next sample before drawing a threshold from it.

**context-ladder (JEV).** No tier accepted any of the 10 planted wrong-tag controls (largest control score 0.09, 0.07, 0.17 for T0, T1, T2), and
real-versus-control separation was high at every tier (AUC 0.963, 0.956, 0.928). Real items accepted at 0.5: 9, 12, 12 of 34. Tiers disagree on real items:
T0 against T2 flipped 5 verdicts (mean probability change 0.146), T1 against T2 flipped 4 (0.095). On the 15 adjudicated items (the 7 disagreements plus 8
random unanimous ones; labels are Claude's, unreviewed by the maintainer; see `gold.json`), accuracy was **T0 10/15 (0.67), T1 11/15 (0.73), T2 13/15 (0.87)**.
The disagreement items are the hard ones, so these are lower bounds, not estimates of general accuracy.

| Tier | Tokens per item | Items per call (58k ceiling) | JEV calls to backfill 127,719 candidates |
|---|---|---|---|
| T0 matched span | 163 | 344 | 372 |
| T1 +-45 s window | 428 | 131 | 975 |
| T2 whole chapter | 2,062 | 27 | 4,731 |

Escalation simulation on the same items: judging at **T1 and re-judging at T2 when the T1 probability is between 0.3 and 0.7** escalated 10 of 34
real items (29%) and scored 13/15 on the adjudicated subset, the same as T2 for every item; the wider 0.2 to 0.8 band escalated 17 (50%) for no gain. At 29%
escalation the cost is about 975 + 0.29 x 4,731, roughly **2,350 calls against 4,731** for T2 on everything (and against a 1,440-call daily JEV cap). Caveat: 15 labels,
chosen where the tiers disagree.

**adjudicator (Qwen).** 30 of 30 synthetic items correct, in six packs of five (about 760 prompt tokens, 400 to 800 reasoning tokens each).

What to do next: extend `gold.json` with maintainer-verified labels (the weekly audit promotes clear-cut items into it), grow the question-type sample, and
re-run `context-ladder` so the tier choice rests on more than 15 labels.
