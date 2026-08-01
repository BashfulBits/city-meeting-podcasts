# Agenda-chapter research tools

Read-only, local tools supporting the empirical work for GH#1078. They may fetch public sources
or local object-store artifacts when explicitly requested, but never mutate episode records,
durable artifacts, feeds, or provider state.

The tools are intentionally kept outside the production pipeline. They create reproducible
evidence for later admission decisions; an experiment result is not a runtime feature.

## Tool groups

- `audit_chapters.py`, `pull_alignment_records.py`, and `build_chapter_alignment_dataset.py`
  establish chapter prevalence and a source-backed benchmark corpus.
- `build_locator_benchmark.py` selects a UID-deduplicated, duration/body-stratified canonical
  timing cohort and measures the existing word-sidecar/VTT locator request without model calls.
  Pass `--include-vtt-fallback` to include synced VTT-only rows and force one per provider when
  available; placeholder/empty agenda artifacts are retained as explicit eligibility diagnoses.
- `select_*`, `run_agenda_llm_shadow.py`, `mistral_agenda_batch.py`, and `evaluate_*` create and
  score frozen shadow experiments.
- `build_agenda_gold_review.py`, `compile_*`, `merge_*`, `prepare_*`, `refresh_*`, the HTML
  files, and `serve_*` implement blinded human source review. Keep unblinding keys separate from
  a reviewer.
- `train_chapter_title_ranker.py`, `predict_agenda_candidate_classifier.py`, and
  `build_agenda_hybrid_hints.py` preserve rejected weak-label baselines. They must not supply
  production LLM prompts or publication decisions.

See `review/40-generated-agenda-chapters.md` for experiment protocol, results, exclusions, and
the next approved decision gates.
