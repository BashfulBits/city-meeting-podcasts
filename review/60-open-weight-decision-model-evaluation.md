# 60 — Open-weight decision model evaluation

Status: **L1 — plan approved 2026-11-17; the local screen, labeling and Phase 0 are done (§8); Phase 1 (streaming loader) is next.**
Depends on [`review/59`](59-deferred-decision-batches.md) (the GPU launcher in PR #2695). This
document does not change that PR; the loader, the Karpo adapter and the evaluation each land in
their own PR.

## 1. Question

Can Cloud Run GPUs give Citypods (a) a quality fallback if OpenRouter drops the Perplexity Decider
route, as BeatAPI dropped JEV, and (b) an independent model voice from a different lineage than
Gemini, at a cost that fits the credit budget? Which open-weight decision models earn a place in
the judge lane (A) and in multi-question tagging (B)?

## 2. Policy decision

Gemma-based decision models are a **separate model family** from Gemini. The maintainer decided
this on 2026-11-17. `config/site_config.yml` still labels both `google`; the relabel (a `gemma`
family) is made in the follow-up PR after the Phase 4 results, not by this plan. Qwen-based routes
(GLiDE, Karpo 4B/9B, elephant-1, Perplexity) remain one family, so they provide fallback capacity,
not independent votes.

## 3. What is known (sources, 2026-11-17)

Scores are Decision Index public suite 0.3 unless marked; "full" is the board 0.4.1 balanced skill
score. They come from the [leaderboard data](https://huggingface.co/spaces/multimodalart/jev-decision-index/blob/main/data/index.json)
and, for Karpo 4B, 9B and 12B, the author's own chart on the model card (the author's batched
runner, not an independent board run).

| Model | Base | Public | Full | New-domain | ECE | Brier | Fits |
|---|---|---:|---:|---:|---:|---:|---|
| Karpo 31B | Gemma 4 | 75.0 | 73.86 | 61.5 | 0.048 | 0.240 | RTX |
| GLiDE 27B | Qwen | 72.5 | 73.37 | 61.6 | 0.021 | 0.251 | RTX |
| Copernicus-31B | Gemma 4 | 70.4 | 72.11 | 62.1 | 0.018 | 0.269 | RTX |
| elephant-1 (9B) | Qwen3.5 | 72.2 | n/p | n/p | n/p | n/p | L4 |
| Karpo 9B | Qwen3.5 | 69.9 | n/p | n/p | n/p | n/p | L4 (8,192-token budget) |
| Karpo 12B | Gemma 4 | 69.8 | n/p | n/p | n/p | n/p | RTX BF16; L4 only via 8-bit |
| GLiDE 9B | Qwen3.5 | 68.8 | 62.48 | 43.2 | 0.017 | 0.282 | L4 |
| Karpo 4B | Qwen3.5 | 67.6 | n/p | n/p | n/p | n/p | L4 |
| Perplexity v1.3 (27B) | Qwen | 69.7 | 67.67 | 55.8 | 0.042 | 0.272 | OpenRouter |
| Liquid d1-2B | LFM3-VL | 51.1 | 34.39 | 13.3 | 0.012 | 0.366 | L4 |
| Liquid d1-3B | LFM2.5-VL | 49.0 | 31.12 | 11.5 | 0.047 | 0.390 | L4 |
| GLiNER2.5-Decide | GLiNER2 | 11.5 | 10.14 | 4.9 | 0.088 | 0.705 | CPU |

`n/p` = not published. The board treats scores within 0.9 points as tied. Public scores can be
tuned to the benchmark; GLiDE 9B fell from 68.8 public to 62.5 full on the new-domain gap, and
civic transcripts resemble a new domain, so the plan measures on our data.

**Gemma-based** (all `gemma` family by §2): Karpo 12B and 31B, Copernicus-31B, S1 26B-A4B, FlexDM,
Jeffan-31b, Sieve 31B, KnowLine 31B and 26B-A4B, OneJev1.6g-31B, oak 1.1, mogan and AJev. No Gemma
model is below 12B (12B, 26B-A4B and 31B only), and none fits an L4 in BF16. The only L4 path is
Karpo 12B in 8-bit, which is unproven (§5, Phase 0).

**Mistral lineage:** ArseneLupinstral v1.0 (Ministral-3-8B, 17.9 GB BF16, about 18 GB at run time,
fits an L4 tightly). It is the only candidate from a third lineage. It runs one forward pass per
option, so it is not a multi-question model.

**Other small lineages:** Liquid d1-2B and d1-3B (LFM), MiniCPM5-2B-Jev (OpenBMB base).

### Multi-question and multi-select

- **One-pass multi-question** (one state, many questions, no per-question re-read): Karpo (all
  sizes; branch attention, 32,768-token context, a longer request splits by question with the full
  state in each part), Liquid d1 (`system_one`), MiniCPM5-2B-Jev (KV-cache branching). GLiDE and
  ArseneLupinstral do not: GLiDE runs one forward pass per question.
- **Native multi-select:** Liquid d1-2B (`choice` with `multiple: true`, returning a probability
  per option). GLiNER2.5-Decide has multi-label by threshold. Karpo's README says `box` questions
  return HTTP 422. No other model was verified to have it. Perplexity's multi-decision behavior was
  not checked here.
- **Emulation:** one yes/no question per label works for every model, at one pass per label except
  where one-pass multi-question applies. For 40 tags over a 1,500-token chapter, GLiDE needs about
  64,000 tokens and Karpo about 3,500–5,500 (branch size assumed at 50–100 tokens; to be
  measured).

## 4. Candidate matrix

**A — judge lane:** Perplexity v1.3 (OpenRouter), Gemini-decision-1 (API), Karpo 31B (RTX), GLiDE
27B (RTX), Copernicus-31B (RTX), Karpo 12B (BF16 on RTX; 8-bit on L4), Karpo 9B (L4), elephant-1
(9B, L4), GLiDE 9B (L4), Karpo 4B or Valen 4B (L4), Liquid d1-2B, Liquid d1-3B, ArseneLupinstral,
MiniCPM5-2B-Jev.

**B — tagging (multi-question in one pass):** Perplexity (baseline), Karpo 9B (L4), Karpo 12B
(BF16 on RTX and 8-bit on L4), Karpo 31B (RTX), Liquid d1-2B (screen), MiniCPM5-2B-Jev (screen if
its branching works). GLiDE is removed from B because it has no one-pass multi-question.

**Filtered out:** GLiNER2.5-Decide. It is an extractor and classifier, 11.5 public and rank 293 of
307 on the board, and its own 60.2% is on a different benchmark. A CPU multi-label tag prefilter
would be a separate project.

**MiniCPM repository:** the popular `openbmb/MiniCPM-OCR-Jev` (about 64,000 downloads) is an OCR
correctness scorer on Qwen3.5-2B that needs an image, so it does not apply. The only text-decision
option is `ytbai/MiniCPM5-2B-Jev` (about 91 downloads, last updated 2026-09-28, Apache-2.0), kept in
the local screen only if its runner works unmodified. `usejul/jul-decision-minicpm5-2b` was not
inspected.

## 5. Phasing

Spend cap: **$20** of cloud credit for Phases 0 to 3, approved 2026-11-17. The local screen costs
nothing.

**Phase 0 — Karpo runner spike and the 12B 8-bit gate (about 1 day, $1–2).** Run Karpo's native
runner on the 9B (L4), the 12B (RTX BF16) and the 12B (L4, `--fp8`) with a synthetic multi-question
request. Measure the real readout cost, peak memory and token cost per added branch, and whether
grouped branches match single-question answers. Confirm whether `--fp8` stores FP8 weights; if it
keeps BF16 weights on the GPU, the 12B needs quantize-while-loading, which Phase 1 would build.
Review the code of every `trust_remote_code` model, pin revisions, and read the licenses (Karpo
Community License, LFM Open License v1.0). Gate: if the 12B 8-bit path cannot fit 22 GiB, report
before building more for it.

**Phase 1 — generic streaming loader (own PR, 2–3 days, $3–5).** Make the GLiDE streaming sink
model-neutral for safetensors, covering Gemma and non-GLiDE Qwen (Karpo 9B and elephant-1 have the
largest relative gain). Optionally quantize to FP8 per tensor while loading. Add the Karpo and
elephant-1 adapters and calibration rows, and re-measure with the real readouts. Gate: loads of at
least 150 MB/s and every matrix model fits its target GPU. Expected: Copernicus-31B load 501 s to
about 300 s, Karpo 9B on L4 323 s to about 80 s (an estimate from the GLiDE path's 170–200 MB/s).

**Phase 2 — datasets (about 2 days, no GPU cost).** Start from the existing `evals/judge` gold
(`gold.json`, `tag/gold.json`, `moment/gold.json`) and report coverage before building anything
new; ask the maintainer if more labels are needed. A: frozen items per judge decision. B: chapters
with the full tag taxonomy as one question per tag. Split dev and holdout; the holdout is untouched
while tuning. Several hundred items per lane; report paired bootstrap confidence intervals.

**Phase 3a — local screen of the small models (no cloud cost).** Liquid d1-2B, d1-3B,
ArseneLupinstral and MiniCPM5-2B-Jev run with the authors' own runners on the maintainer's M1 Max
(32 GB, MPS) over the dev set, each in a throwaway virtual environment with no credentials and
after reading the author code. A model is promoted to Phase 3 only if its dev accuracy is at least
90% of Perplexity's and its invalid-output rate is under 2%. This screen cannot measure cloud speed
or cost; the d1 and MiniCPM size class is close to the measured d1-3B calibration (about $0.04 per
200k tokens on an L4).

**Phase 3 — cloud runs.** Each model runs on its cheapest fitting GPU through the launcher in a
separate ledger window. Metrics: accuracy against gold, calibration (ECE and Brier), agreement with
Perplexity, failure and invalid-output rate, cost per valid decision, and tokens per tag in B.

**Phase 4 — decision memo.** Recommended Perplexity fallback, recommended L4 and RTX routes, and
the `gemma` family relabel follow-up PR.

## 6. Expected cost (list rates, estimates)

| Model | Per 200k tokens | Per 2M tokens |
|---|---:|---:|
| GLiDE 9B (L4) | $0.07 | $0.36 |
| Karpo 9B (L4) | $0.13 | $0.34 |
| elephant-1 (L4) | about $0.1 est. | about $0.35 est. |
| Karpo 12B 16-bit (RTX) | $0.31 | $0.49 |
| Karpo 12B 8-bit (L4) | about $0.09 est. | about $0.42 est. |
| GLiDE 27B (RTX) | about $0.5–0.6 est. | about $0.9 est. |
| Karpo 31B (RTX) | about $0.6 est. | about $1.0 est. |
| Copernicus-31B (RTX) | $0.58 | $1.00 |
| Liquid d1-3B (L4) | $0.04 | $0.11 |

Measured rows come from the launcher calibrations in review/59 (backbone-only timings; billing is
list price, unreconciled). A full A pass is about $3.5 and a full B pass about $2; reruns and
development bring Phases 0 to 3 to about $15–20. Perplexity (OpenRouter) is free; Gemini-decision-1
is unpriced.

## 7. Risks and unknowns

- Karpo 12B 8-bit on an L4: **resolved by Phase 0** (fits at 12.9 GiB peak, via a layer-wise quantize that the
  author's `--fp8` order does not do; §8). Remaining risk: it needs about 24 GB of host RAM to load
  until Phase 1's streaming loader lands.
- Public scores may not hold on civic text; the d1 models score far below the rest (full 31–34
  against 62.5 for GLiDE 9B and 67.7 for Perplexity v1.3) and earn a place only through the screen.
- Four runtimes need author code (Karpo, d1, ArseneLupinstral, MiniCPM); the screen keeps adapter
  work to promoted models.
- Karpo's runner targets CUDA and the 31B will not fit in 32 GB, so those stay in the cloud.
- Licenses: Karpo (weights and inference code) is Apache-2.0 as published 2026-11-18, with attribution
  appreciated but not required (the earlier note of a Community License was wrong or out of date; re-check at
  pin time), LFM Open License v1.0, ArseneLupinstral and MiniCPM not yet read, StartLux and Nimble are
  CC-BY-NC.
- Costs are estimates until reconciled against billing.

## 8. Results

Each phase appends its result here with the date, the command, the model revision and the evidence
file.

**2026-11-17 — label inventory (Phase 2 precheck).** `evals/judge/tag/gold.json` and
`evals/judge/moment/gold.json` are empty lists (review/53 PR6 starts them empty and fills them from
the weekly blind audit). `evals/judge/gold.json` holds truth for 30 synthetic bundling items (easy)
and 10 controls (real excerpts paired with an unrelated tag, which must be rejected); the 34 real
context-ladder items and the 24 real pull quotes have no human labels. The `chapter-agenda`,
`chapter-locator` and `remedy` gold files cover other tasks. So the existing gold cannot measure
accuracy on civic tagging or judge decisions. More labeled data is needed before Phase 3; the
decision is the maintainer's (a labeling pass or a promoted audit set).

**2026-11-17 — author code in the local screen.** The d1 repositories ship about 20 Python files of
custom model code, ArseneLupinstral ships an `arsenelupin` package, and MiniCPM5-2B-Jev loads
`head.pt` (a PyTorch pickle, so unsafe to load without `weights_only=True`) plus `model.py`. Running
downloaded author code needs the maintainer's explicit approval per repository; nothing was
downloaded or run before that decision.

**2026-11-17 — author-code risk review (static; nothing executed).** Text and code files only were
fetched; weights were not.

| Item | Finding |
|---|---|
| ArseneLupinstral code (`arsenelupin`, 14 files, about 1,700 lines) | No `exec`, `eval`, `subprocess`, `pickle` or hidden network calls. The only network use is `gguf.py` posting to a configured llama-server URL (optional 8-bit path) and `serve.py`; loads use `local_files_only` unless `allow_download` is set. Environment variables only set limits and an optional API key. `serve.py` binds 127.0.0.1 by default and refuses a non-local host without an API key. |
| ArseneLupinstral weights | bfloat16 safetensors (17.9 GB), `readout/head.safetensors` (safe). `tete.py::lit_etat_tete` falls back to `torch.load(weights_only=False)` only for a path not ending in `.safetensors`; avoid that path. Pins `transformers==5.17.0`. Apache-2.0 on a Ministral-3-8B Apache-2.0 base. |
| MiniCPM5-2B-Jev code (`model.py` 915 lines, `serve.py` 147) | No `exec`, `eval`, `subprocess` or network calls beyond Hugging Face loads of the openbmb base; `head.pt` is loaded with `weights_only=True`; `serve.py` binds 127.0.0.1. |
| MiniCPM `head.pt` | Disassembled statically with `pickletools`: the only globals are `collections.OrderedDict`, `torch._utils._rebuild_tensor_v2` and `torch.FloatStorage` (standard tensor pickle). Base `openbmb/MiniCPM5-2B` is Apache-2.0 and well used (about 987,000 downloads). |
| Maturity (the larger risk) | ArseneLupinstral: 17 downloads, 0 likes, last updated 2026-09-25, French-language identifiers, not on the board. MiniCPM5-2B-Jev: 91 downloads, 12 likes, last updated 2026-09-28, not on the board. Quality claims are the authors' own on their own benchmarks. |

Mitigations if either is run: pin the revisions (ArseneLupinstral `409d8945d858`, MiniCPM5-2B-Jev
`27afc6f17ceb`); verify the repository's `SHA256SUMS`; add the package source to `PYTHONPATH`
instead of `pip install`; set `HF_HUB_OFFLINE=1` after download; run in a throwaway virtual
environment with no credentials; never start `serve` or the GGUF path; use only the safetensors
head. Residual risk is low for execution and moderate for usefulness.

**2026-11-17 — local screen, Liquid d1 (Phase 3a, first half).** Runner and scorer:
[`run`](evidence/2026-11-17-d1-local-screen-run.py),
[`score`](evidence/2026-11-17-d1-local-screen-score.py); raw answers:
[d1-2B](evidence/2026-11-17-d1-2b-local-screen.json),
[d1-3B](evidence/2026-11-17-d1-3b-local-screen.json). Revisions `a6bce774` (d1-2B) and `05b80faf`
(d1-3B), bfloat16 on an Apple M1 Max (MPS), authors' own `system_one`, no network after download, a
throwaway virtual environment with transformers 5.23. Inputs are the frozen `evals/judge` set from
review/49: 30 synthetic bundling items in two layouts (truth known) and 44 context-ladder items at
three tiers (10 controls that must be rejected; the 34 real items have no human labels, so JEV's
2026-09-30 answers are the reference and are not truth). All answers were valid (0 invalid of 592
questions per model).

| | d1-2B | d1-3B | JEV (reference) |
|---|---|---|---|
| Bundling, evidence in each question | 30/30 | 30/30 | 30/30 |
| Bundling, evidence in shared state (one pass) | 24/30 (AUC 0.93) | 29/30 (AUC 1.00) | 30/30 |
| Controls accepted at 0.5 (T0, T1, T2; 10 each) | 0, 1, 0 | 0, 0, 0 | 0, 0, 0 |
| AUC, real items vs controls (T0, T1, T2) | 0.97, 0.90, 0.77 | 0.98, 0.97, 0.87 | 0.98, 0.97, 0.90 |
| Real items accepted at 0.5 (T0, T1, T2) | 77%, 68%, 53% | 65%, 65%, 50% | 41%, 41%, 44% |
| Verdict agreement with JEV on real items (T0, T1, T2) | 65%, 74%, 91% | 65%, 76%, 88% | n/a |
| Spearman versus JEV (T0, T1, T2) | 0.83, 0.87, 0.91 | 0.59, 0.66, 0.85 | n/a |
| Wall time, 44 T2 items (9 calls) | 91 s | 501 s | n/a |

Reading: neither model is broken (no invalid output, controls rejected), and d1-3B separates real
items from controls nearly as well as JEV. Both accept far more real items than JEV (50–77% against
41–44%); without labels, whether that is recall or false positives is unknown. d1-2B is weaker on
long context (T2 AUC 0.77) and on the shared-state layout. Multi-select was not exercised. The
sample is 30 synthetic items and 10 controls, so this is a sanity screen, not a quality measurement;
the promotion bar (at least 90% of Perplexity's accuracy) needs the labeled set. A first run of
d1-3B failed offline (the model code resolves its tokenizer by repository name, and a pinned download
leaves no `main` ref); it was rerun from the local snapshot path.

**2026-11-17 — local screen, ArseneLupinstral and MiniCPM5-2B-Jev (Phase 3a, second half).** Same
inputs, scorer and machine as the d1 screen. Runners:
[Arsene](evidence/2026-11-17-arsenelupinstral-local-screen-run.py),
[MiniCPM](evidence/2026-11-17-minicpm-local-screen-run.py); raw answers:
[Arsene](evidence/2026-11-17-arsenelupinstral-local-screen.json),
[MiniCPM](evidence/2026-11-17-minicpm5-2b-jev-local-screen.json). Revisions: ArseneLupinstral
`409d8945d858` (repository `SHA256SUMS` verified), MiniCPM5-2B-Jev `27afc6f17ceb` on base
`openbmb/MiniCPM5-2B` `42433e90dad9`. Each ran in a throwaway virtual environment with transformers
5.17.0 (Arsene, as it pins) or 5.23 (MiniCPM), `HF_HUB_OFFLINE=1`, the package source copied from the
verified snapshot, no server started and no credentials. Arsene used its shipped
`calibration/default.json`, the `independent` engine mode and the `blind` formulation
(`shared_prefix`, the shipped default, was not tried). All answers were valid (0 invalid of 296
questions each).

| | MiniCPM5-2B-Jev | ArseneLupinstral | d1-3B | JEV (reference) |
|---|---|---|---|---|
| Bundling, evidence in each question | 30/30 | 30/30 | 30/30 | 30/30 |
| Bundling, evidence in shared state | 21/30 (AUC 0.93) | 26/30 (AUC 1.00) | 29/30 | 30/30 |
| Controls accepted at 0.5 (T0, T1, T2) | 0, 0, 1 | 0, 0, 0 | 0, 0, 0 | 0, 0, 0 |
| AUC, real vs controls (T0, T1, T2) | 0.92, 0.84, 0.79 | 0.98, 0.96, 0.84 | 0.98, 0.97, 0.87 | 0.98, 0.97, 0.90 |
| Real items accepted (T0, T1, T2) | 50%, 53%, 50% | 29%, 32%, 38% | 65%, 65%, 50% | 41%, 41%, 44% |
| Verdict agreement with JEV (T0, T1, T2) | 74%, 82%, 88% | 76%, 85%, 88% | 65%, 76%, 88% | n/a |
| Spearman versus JEV (T0, T1, T2) | 0.74, 0.81, 0.88 | 0.67, 0.75, 0.89 | 0.59, 0.66, 0.85 | n/a |
| Wall time (44 T2 items; 30-item shared-state bundling) | 112 s; 11 s | 1,758 s; 457 s | 501 s; 7 s | n/a |

Reading: both ran cleanly and rejected the planted controls. ArseneLupinstral separates real items
from controls about as well as d1-3B and JEV at the short tiers and is the strictest model tested
(it accepts 29–38% of real items against JEV's 41–44%), but it is by far the slowest locally (29
minutes for one tier on an M1 Max; a cloud GPU would change that). MiniCPM5-2B-Jev is fast and is
the weakest of the four on separation (T2 AUC 0.79, one control accepted) and on the shared-state
layout (21 of 30). The same caveats apply as for d1: the sample is 30 synthetic items and 10
controls, the real items are unlabeled, and agreement with JEV is not accuracy. By this screen all
four small models are viable-looking but unproven. The differences between them are small enough that
only the labeled set can rank them, and the promotion bar (at least 90% of Perplexity's accuracy)
stays open until that sample exists.

**2026-11-17 — production judgment inventory (Phase 2, read-only).** Run with `citypods-env` and
`b2_from_env()`: listed `state/` (120,365 objects, 5.6 GB; nothing written) and read the 48
`state/sources/*/episodes.json` files one at a time, keeping only each judged subject's judgments
(model, role, tier, question, value, date, evidence digest). The compact extract (84,091 subjects,
105 MB) stays outside the repository because it is derived from production state.

| | |
|---|---|
| Judged subjects | 84,091: 71,026 `tag`, 13,065 `moment`, across 42 sources and about 7,800 tag episodes |
| Judges (subjects judged) | `typesafe/jev-1.13` 78,070; `openrouter/nvidia/nemotron-3.5-super-vl:free` 74,658; `gemini/gemini-decision-1` 74,649; `google/gemma-4-31b-it` 50,797; `zai/glm-5.3-flash` 27,355; `deepseek/deepseek-v4.1-flash` 1,575; `qwen/qwen3.8-27b` 645; `perplexity/pplx-decider-v1.3` 232 |
| Judges per subject | 1: 7,852; 2: 3,564; 3: 17,713; 4: 34,948; 5: 20,014 |
| Window | judgments dated 2026-10-10 to 2026-11-17 |
| Tag subjects with at least 3 binary verdicts on `supported` | 67,804: 19,807 unanimous yes (29%), 23,133 unanimous no (34%), 24,864 split (37%); split cases are mostly 3-of-5 (4,276), 2-of-5 (3,834), 1-of-5 (3,758) and 4-of-5 (3,337) |

Consequences for the labeled sample: (1) there is enough multi-judge material to label a
stratified sample without building new model runs; (2) Perplexity has judgments on only 232 subjects
and none on tag `supported`, so its baseline has to be generated on the sample (OpenRouter, free
route); (3) JEV (`jev-1.13`, retired as a free route) and Nemotron are in most subjects and serve
as a reference, but the human label is the truth; (4) the existing weekly blind audit (review/49
section 4a, at most 12 items a week, hides every verdict) is far too slow for a several-hundred-item
set and is a different design from a suggestion-assisted pass, so the evaluation set is a separate
file and does not enter `evals/judge/*/gold.json` until reviewed.

**2026-11-17 — labeling sample built (Phase 2).** Decisions (maintainer): 300 + 60 items; moments
included; **fully blind** (revised the same day): no production verdicts, no pre-checked tags and no
suggestion badges are shown. The only assist is that lane B shows a 12-tag list instead of the full
taxonomy of 40+ tags. Built read-only from production state (seed 20261117; the sample stays outside
the repository until labeled, then freezes into `evals/`).

- **Lane A (300 items):** 240 tag subjects (80 unanimous-yes, 80 unanimous-no, 80 split) and 60
  moment subjects (20 each), each with at least 3 production judges. At most 3 items per episode per
  stratum and no source above 20%; the largest source holds 15.7%. The evidence text is the T1 text
  the judges were shown, rebuilt with the repository's own `build_episode_texts` and evidence
  builders (with the wider T2 window on request), so a model under test sees the same input. The
  question is the production rubric: Supported, Not supported or Uncertain. Production verdicts are
  kept in the sample file for analysis and are not in the labeling page.
- **Lane B (60 chapters):** 10 chapters each with 1 to 6 production-suggested tags (at most 2 per
  episode, 20 per source). The largest suggestion count is 6, so every chapter shows exactly 12 tags
  (the union of the production suggestions, similar tags from the same taxonomy group, and random
  tags: 210, 345 and 165 across the set), at most 2x the largest suggestion count, and nothing in the
  page says which are which. Chapter text is capped at 1,800 words.
- **Tool:** a local server (loopback only) serving a single HTML page; every answer is posted to the
  server and saved to disk immediately (`labels.json` plus an append-only log), with browser storage
  as a backup. Tested in the in-app browser: answering, tab switching, saving and reload all work.
- **Analysis notes:** weight lane A back to the real stratum mix; Perplexity must be run on the
  sample (it has no production tag `supported` judgments); because labels are blind, human-versus-judge
  agreement needs no anchoring correction.

### Result 2026-11-17: moment labels compromised by an evidence-window defect

The maintainer reported that several moment quotes read as correct and well reasoned but were not
inside the example passage. Checked against the sample and the production transcripts:

- In 34 of 60 moments the quote text is absent from the passage the judge (and the labeler) saw,
  including the wider T2 window. 32 of the 33 `not supported` and `uncertain` human labels are in
  this group. In the 33 of those 34 transcripts that were checked, the quote exists in the transcript.
- Cause: provider-aligned transcripts carry whole minutes of speech in a single segment. The moment
  evidence builder takes the segments overlapping quote ±60 s, joins them, and keeps the first
  `CONTEXT_WORD_CAP = 400` words (`citypods/judging/moment_task.py`). When a long segment starts
  well before the quote, the cap cuts the quote off.
- Consequences: (1) production moment judgments on such transcripts are likely wrong, and the
  judges' `no` verdicts for them are partly an artifact; (2) the human `not supported` labels on
  these items measure the defect, not the model, so they are excluded from the moment accuracy
  comparison until the passages are rebuilt around the quote and the items are re-reviewed.
- Item ids: `evidence/2026-11-17-moment-window-defect-items.json`. The product fix (center the cap
  on the quote, or use word-level timing from `words_key`) is a separate change.

### Result 2026-11-17: lane B chapter tags chosen by Claude, not by the maintainer

The maintainer asked Claude to choose the tags for the 60 chapters. Choices come from the blind
12-tag list, each made from the chapter text (up to 6.5k characters: first 4.5k plus last 2k). They are
in `evidence/2026-11-17-claude-chapter-labels.json`, kept apart from the human labels
(`evidence/2026-11-17-human-labels-lane-a.json`). Treat them as a model-made reference, not
human ground truth: they may share biases with the models under test and are not independent of the
LLM lineage question. Spot-check a sample before using them for any decision.

### Result 2026-11-17: the 34 defective moment items, rebuilt and re-scored by Claude

At the maintainer's request, Claude rebuilt the 34 passages around the quote (130 words either side,
found by matching the quote's word sequence in the served transcript) and re-scored them on the
production rubric ("does the quote, as spoken, say what the stated reason claims?").
Per-item results and notes: `evidence/2026-11-17-claude-moment-rescore.json`.

- 33 `supported`, 1 `not_supported` (the reason attributes the vote explanation to the Mills Road
  annexation, but the quote belongs to the preceding item). The maintainer will spot-check.
- Where the reason names a speaker role, pond or similar that is not visible in the window, Claude judged
  the quote's claim only and noted it; role attributions were not independently verified.
- Merged moment reference set: the maintainer's 26 intact-passage labels plus these 34 Claude labels give
  58 supported, 1 not supported, 1 uncertain. Nearly everything is supported, so moments cannot
  separate judges (the bar is too low). They remain useful for one purpose: showing which judges
  wrongly reject correct quotes when given a truncated passage. The 20-item `no` stratum was almost entirely
  the window defect.
- Provenance: these 34 labels are model-made (same caveats as the chapter labels above).

### Result 2026-11-18: Phase 0 (Karpo runner spike and the 12B 8-bit gate)

**Gate: passed.** Karpo 12B in FP8 fits an L4 with room to spare. Evidence in `review/evidence/2026-11-18-karpo-*`,
the spike script (`...-phase0-spike.py`) and its Dockerfile.

*Code and licence review (pinned).* Runner `machinepulse-ai/karpo-decision` at commit
`6600011eb5cf46232d056ec0717dc726146d29de` (tarball sha256 `ba814705…defb4`), model revisions 12B
`664640316919…`, 9B `0fb729874694…`. Static audit of `src/`: no exec, eval, subprocess, pickle, ctypes or
network client; the HTTP server binds 127.0.0.1 by default and the spike used only the Python API. The model repos contain no code
(`trust_remote_code` is not used). The runner needs torch ≥ 2.14, transformers ≥ 5.17 and Python ≥ 3.12, which match the
existing worker lock; it was added to the worker image by source with `PYTHONPATH` plus `pybase64` and `torchvision 0.29.0`
(`--no-deps`). Licences: Apache-2.0 (model, code), Gemma 4 notice kept.

*What `--fp8` does.* It stores FP8 (e4m3) weights with a scale per output row for every decoder projection
(328 of them in the 12B) and runs them through `torch._scaled_mm`. But it quantizes **after** the full BF16 model is on the GPU
(`load_decider`, then `quantize`), so the peak is the BF16 size: 22.35 GiB of weights alone on a 22.03 GiB L4. The author's
order therefore cannot run the 12B on an L4. Quantizing one decoder layer at a time (load on CPU, move a layer, quantize it) does:
12.15 GiB resident, 12.87 GiB peak while answering a 6.3k-token request. The extra cost is about 24 GB of host RAM while loading
(the job used 32 GiB; resident peak 9.6 GiB) and about 250 s to read and quantize the weights through the FUSE mount (Phase 1's
streaming loader replaces this).

*Measured (38 tag questions per chapter, one request, warm):*

| Run | Weights | Peak | s per chapter | Notes |
|---|---:|---:|---:|---|
| 12B BF16, RTX PRO 6000 | 22.35 GiB | 23.17 GiB | 0.32–0.53 | does not fit an L4 |
| 12B FP8, RTX PRO 6000 | 12.17 GiB | 12.89 GiB | 0.23–0.39 | about 30% faster than BF16 |
| 12B FP8, L4 | 12.15 GiB | 12.87 GiB | 3.3–5.0 | about $0.001–0.0015 per chapter at the list rate |
| 9B BF16, L4 | 15.65 GiB | 16.34 GiB | 0.9–1.2 | fla autotuning added 8–19 s on some new shapes (no cached configs in this image) |

*Branch cost.* One added question costs about 93 tokens on the Gemma 12B tokenizer (820 tokens for one question with a 150-word state,
1,324 for five, 1,791 for ten, 2,713 for twenty) and about 80 on the Qwen 9B. All 38 tags over a short chapter take about 4,150–4,570
tokens (3,050–3,450 on the 9B), 6,279 for an 1,800-word chapter. This matches the plan's 50–100 token assumption. Compared with GLiDE's 64,000
tokens for 40 tags over a 1,500-token chapter, one-pass emulation is about 10× cheaper in tokens.

*Agreement.* FP8 against BF16 on the same 304 chapter-tag probabilities (8 chapters × 38): mean absolute difference 0.0042, maximum 0.16, 2 decision flips
at 0.5 (0.7%). L4 FP8 against RTX BF16: mean 0.0033, maximum 0.078, 1 flip. **Grouped against single-question answers differ more**
(12 pairs: mean 0.046, max 0.49, 1 flip on the 12B; mean 0.054, max 0.32, 1 flip on the 9B), because each question sees the list
of all the other questions; a grouped run is a different measurement, not the same one faster. That is a quality question for Phase 3, which will compare
both formats against labels.

*Not run.* The naive `--fp8` order on an L4 was not executed: the BF16 weight size alone exceeds the GPU. The 31B and the HTTP
server were not tested. Costs: about $1.5 at list rates (RTX run about $0.7 including two aborted starts, L4 runs about $0.15 each, builds
and a 24 GB cross-region copy under $0.4), recorded against the $20 cap; cloud billing not yet reconciled.
Left in place (not deleted): jobs `r60-karpo-spike-l4` (us-east4) and `r60-karpo-spike-rtx` (us-central1), images
`r60-karpo-spike-v1..v3`, and the 12B copy in the us-east4 model bucket.
