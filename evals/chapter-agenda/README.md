# chapter-agenda evaluation set

The first committed per-task evaluation set (GH#1852). It scores a model on the `chapter-agenda`
task: given a meeting's agenda text, extract the actual agenda action items.

## Ground truth

`gold.json` holds the **meeting provider's own published chapters** for each episode (Granicus,
Swagit and CivicClerk publish them; no other provider does today). They are independent of any
model. `manifest.json` holds only the inputs -- the agenda text exactly as production reads it --
and never the chapters.

## Selection (version 1, frozen 2026-09-24)

29 episodes, 376 provider chapters: Granicus 12, Swagit 12, CivicClerk 5 (all CivicClerk episodes
that qualified). Chosen deterministically by the locator research selector
(`scripts/research/agenda_chapters/`: provider x meeting-length x body diverse, most recent
first), keeping only episodes whose provider chapters are well-formed and whose agenda sidecar is
complete (the same checks as `build_locator_dataset.py`). Skipped UIDs and reasons are recorded in
`manifest.json`. Re-freezing (`scripts/eval_chapter_agenda.py --freeze`) is a reviewed change that
bumps `version`; results always record the set version they used.

Known limits: three providers only; agendas are mostly short (median ~600 tokens, max ~12k), so
long-agenda behavior is under-represented.

## Holdout split (frozen 2026-09-24)

`holdout/` holds 24 more episodes (304 provider chapters; Granicus 12, Swagit 12; CivicClerk had no
further qualifying episodes), disjoint **by meeting** from the main set. Rule: nobody inspects
holdout outputs while designing a change (a validator repair, a prompt edit). A change is accepted
only if it helps on the main set **and** does not hurt on the holdout; a change that only fits the
main set's meetings shows up there. Limit: few providers publish chapters, so the splits share most
cities (5 of the holdout's 8 feeds also appear in the main set), and holdout agendas share those
cities' layouts. `--split holdout` runs it; results go to `results/<date>-holdout.json`.

## Method

`scripts/eval_chapter_agenda.py --model <model>`:

1. builds production's exact request (`citypods.chapter_jobs.build_agenda_job`);
2. calls that one model directly -- no pool, backups or queue -- with every provider serving it
   paused on the v2 dispatch Worker, so production traffic cannot distort the run; provider
   errors are retried, then recorded as *unanswered*;
3. post-processes with production's `finalize_agenda_job` (strict validation plus the recovery
   layer); a response it rejects is *invalid*;
4. matches generated items to provider chapters with the original benchmark's matcher
   (`audit_locator_crosswalk._pair_features` / `_chapter_status`, thresholds 0.60 / 0.82).

| Metric | Definition |
|---|---|
| valid_rate | answered episodes whose output passed finalization / answered episodes |
| recall | provider chapters with a strong or possible match / provider chapters (valid episodes) |
| precision | items matched one-to-one to a provider chapter / items that match any chapter |
| extra_items | finalized items with no provider chapter (reported, not counted as errors) |
| f1 | harmonic mean of precision and recall |

A model with more than 10% unanswered episodes is *inconclusive*, not a result.

Why precision ignores unmatched items: `finalize_agenda_job` rejects any item whose evidence is
not in the agenda text, so every scored item is a real agenda item. Providers do not publish a
chapter for every agenda item (consent items, for example), so an item without a chapter is
not a mistake, and counting it as one would reward models for extracting less. The matcher's own
ambiguity rule still penalizes duplicated or vague items: a chapter whose top two items score
within 0.08 is *ambiguous* and credits neither. The original 2026-09-13 benchmark's precision
definition was not recorded, so its 87.4% is not directly comparable -- every model here, including
the baselines, is re-run on this set.

## Admission rule (maintainer decisions 2026-09-24)

`chapter-agenda` output is upstream of the locator, tags and moments, so correctness comes first.

- **Validity:** at least 95% valid after at most one retry, and at least 80% valid per call. An
  invalid reply is re-dispatched as a fresh job; tested failures were random per call (hy3: 20 of
  21 retries passed), so a single retry clears them, while the per-call floor keeps a chronically
  flaky model from quietly doubling its calls.
- **Quality:** a candidate beats a baseline when its F1 is higher and its precision is not lower,
  on both the main set and the holdout (the holdout split, `--split holdout`, and its results
  arrive with PR #1856, which builds on this one).
- **Models that are about equal share the work:** they go together in the lane's `models` pool,
  so dispatch uses whichever route has capacity.
- **Prefer the lowest general score that does the task well:** between otherwise-equal choices,
  favor the model with the lower Artificial Analysis index, keeping strong general models free for
  tasks that need them.

### Re-scoring a validator change

Every answered episode stores its raw reply. `--rescore results/<file>.json` re-runs production
finalization on those replies with the current code, without calling any model, so a validator
change is compared on identical output: score the same file at the commit before the change and at
the commit after it.

## Results

`results/<date>.json` holds every run: per-model summary and per-episode outcome. The latest
comparison is summarized in the PR or review that acted on it.

### 2026-09-24b (main + holdout; validator repairs; corrected scorer)

These runs predate the single admission retry, so "Valid" below is first-attempt validity (the
per-call floor). Runs from here on also report `valid_rate` after one fresh retry of an invalid
reply and `first_attempt_valid_rate`; the retry test on hy3's failures passed 20 of 21 retries.

Run with stored replies, then scored with the repaired validator (`results/2026-09-24b*.json`).
Every reply was also re-scored with the pre-repair validator; on episodes valid under both, every
score is identical, and no previously valid episode became invalid.

| Model | Split | Valid (before -> after) | Precision | Recall | F1 | Chapters found per answered agenda (before -> after) | Median s |
|---|---|---|---|---|---|---|---|
| Nemotron 3 Ultra | main | 90% -> 100% | 0.864 | 0.662 | 0.750 | 0.521 -> 0.662 | 57 |
| Nemotron 3 Ultra | holdout | 88% -> 92% | 0.949 | 0.753 | 0.840 | 0.553 -> 0.691 | 44 |
| tencent/hy3 | main | 83% -> 90% | 0.885 | 0.666 | 0.760 | 0.463 -> 0.535 | 37 |
| tencent/hy3 | holdout | 75% -> 83% | 0.909 | 0.828 | 0.867 | 0.572 -> 0.618 | 42 |
| Gemini 3.1 Flash Lite | main | 93% -> 97% | 0.802 | 0.705 | 0.750 | 0.548 -> 0.590 | 7 |
| Gemini 3.1 Flash Lite | holdout | 96% -> 100% | 0.914 | 0.760 | 0.830 | 0.747 -> 0.760 | 4 |
| Gemini 3.5 Flash Lite | main | 72% -> 100% | 0.728 | 0.636 | 0.679 | 0.394 -> 0.636 | 4 |
| Gemini 3.5 Flash Lite | holdout | 96% -> 100% | 0.909 | 0.737 | 0.814 | 0.711 -> 0.737 | 3 |

- The morning run's validity for hy3 (96.6%) was inflated: the harness then retried a reply that
  failed the JSON schema as if the provider were busy. hy3's remaining failures are schema errors
  (`items` not a list, an extra field), which production also retries.
- hy3 under `json_schema` instead of `json_object` was worse on both splits (valid 86% / 67%,
  F1 0.679 / 0.731), so its route keeps `json_object`.
- NVIDIA deepseek-v4.1-flash was not scored: valid JSON only with `prompt_only`, and ~200 s per
  small agenda even with thinking disabled (see review/48).
- Scores differ from the morning table: position-only chapters ("Item 3A") are now matched by
  reference, and single runs vary.

### 2026-09-24 (set v1)

| Model | Answered | Valid | Recall | Precision | F1 | Median s |
|---|---|---|---|---|---|---|
| nvidia/nemotron-3-ultra-550b-a55b:free (primary) | 29 | 86.2% | 0.601 | 0.869 | **0.711** | 56 |
| tencent/hy3 | 29 | 96.6% | 0.608 | 0.795 | 0.689 | 34 |
| gemini/gemini-3.5-flash-lite | 29 | 62.1% | 0.486 | 0.578 | 0.528 | 64 |
| gemini/gemini-3.1-flash-lite | 9 (inconclusive: Gemini 503s) | 88.9% | 0.447 | 0.667 | 0.535 | 376 |
| deepseek/deepseek-v4.1-flash (NVIDIA) | not scored | | | | | |

- hy3 beats 3.5 Flash Lite (also on the 17 episodes both passed: F1 0.677 vs 0.558) and became a
  backup in its place; it does not beat Nemotron (on the 24 both passed: F1 0.631 vs 0.731).
- DeepSeek v4.1 was stopped: NVIDIA returns empty content to any `response_format`, so the run
  measured the request format, not the model. Re-run after review/48 PR C.
- Most invalid responses were validator false positives on grounded items (a composed display
  reference such as `4.A`, straight vs curly quote marks, page footers), and one bad item rejects
  the whole response; see the validator-repair recommendation on PR #1851.
