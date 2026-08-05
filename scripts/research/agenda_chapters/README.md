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
- `build_locator_dataset.py` freezes the next provider-chapter retrieval cohort as separate
  `manifest.json`, `gold.json`, and `diagnostics.json` research artifacts. It keeps normalized
  provider/body families on one split, records start-only provider chapter provenance, and
  revalidates any existing generated agenda evidence with the current post-processor. It never
  calls a model or loads provider chapters into the retrieval input. Use
  `--selection-pool-per-provider` with `--fetch-agendas` when known-bad artifacts must be replaced
  before the final target split is frozen; public Cloudflare-backed sidecars are attempted before
  the B2 fallback.
- `audit_locator_crosswalk.py` is a scoring-only diagnostic: it compares hidden provider chapter
  titles with generated agenda titles/evidence and, when supplied, the original agenda source
  candidates. It reports strong, possible, ambiguous, and unmatched relationships; its output
  must never be passed to a retrieval or production LLM request.
- `evaluate_locator_retrieval.py` compares deterministic lexical, episode-level TF-IDF similarity
  (a lightweight embedding proxy), and their neighboring-window union against hidden provider
  starts. Pass `--baseline-crosswalk` to obtain a paired comparison where recovered candidates
  are scored against the same strict target set. Its report includes both provider-chapter recall
  and candidate-side recall (unique generated candidates linked to one or more strong provider
  chapters), which separates agenda-candidate coverage from transcript retrieval quality. Use
  `--neighbor-radius` to test how much adjacent timed context the union sends around each clue; a
  larger radius increases packet size and is distinct from relaxing `--tolerance`, which only
  changes scoring. It also records full-context request sizing. It is a read-only evaluator, not a
  runtime locator and not a source of provider labels for model prompts.
- `train_transition_scorer.py` is the bounded supervised experiment: it builds agenda-item/timed-
  unit feature rows, uses strong provider-chapter starts as development-only labels, and scores
  every timed unit by default. `--candidate-pool-top-k` enables the separate secondary experiment
  that reranks only the lexical/TF-IDF union; the primary all-unit path can therefore discover
  transitions those deterministic cues missed. It reports learned-only and deterministic-plus-
  learned recall without exposing provider labels to runtime features or prompts. The tool samples
  hard negatives from existing retrieval paths and must remain research-only until grouped
  validation and the frozen held-out test support an admission decision. The optional pairwise
  ranker and adjacent-unit novelty/change-point features are evaluation-only variants; the first
  comparison did not improve the primary HistGradientBoosting result. The optional
  `--speech-rate-mode vector|derivative|both` family uses word-sidecar timestamps to add a fixed
  one-second-binned `-30..+30` second speech-rate shape (and, optionally, its finite-difference
  derivative) with an episode-level robust normalization and an explicit availability flag. It
  defaults to `none`, is unavailable for VTT-only rows, and remains research-only; the current
  development ablation does not change the production scorer or locator request. The separate
  `--transition-phrase-mode learned` family learns 1--3-gram transition cues from development
  provider starts only. Its positive evidence decays with distance to the start, downweights
  post-start content, and is aggregated per episode before log-odds fitting so a long meeting or
  a repeated topic cannot dominate the phrase map. `--exclude-uid` is used to keep checkpoint
  episodes out of both fitting and validation; neither feature family is production-wired.
- `analyze_transition_risk.py` consumes the scorer's development-only per-item diagnostics and
  reports confidence/coverage tradeoffs for a possible compact-route escalation policy. It is a
  measurement tool only; its thresholds must not be copied into production before held-out
  confirmation.
- `train_transition_scorer.py --reconcile-candidate-count` also measures a small order-neutral
  distinct-unit assignment diagnostic. It prevents two agenda items from reusing one timed unit,
  but does not impose monotonic agenda order; the first development result did not improve recall.
- `build_locator_packets.py` builds the paired full, deterministic-compact, and learned-supplemented
  compact request manifests. It stores request hashes/unit IDs and keeps provider-label scoring
  separate from request material. `run_locator_packet_shadow.py` is the bounded model-call runner;
  it validates returned unit IDs and scores anchors only after the response is received. The
  research-only `run_locator_hint_ab.py` compares no-hint, method-bucket, classifier-only, and
  deduplicated pooled full-context hint encodings on a frozen packet sample. It never sends hidden
  provider labels. Non-Mistral variants may be run with bounded parallel workers; Mistral remains
  serial because its account quota and observed latency make concurrent calls unsafe.
- `select_locator_calibration_cohort.py` selects a provider/body/duration-stratified held-out
  confidence cohort. `prepare_locator_calibration_review.py` joins model proposals by
  episode/item/timed-unit evidence reference and includes a small transcript context; it does not
  include provider targets. `serve_locator_calibration_review.py` and
  `locator_calibration_review.html` provide the localhost-only manual adjudication UI. Reviewers
  choose evidence status first, then independently label item correctness and boundary validity;
  decisions and comments stay in a separate research JSON file.
- `run_locator_patient_cohort.py` wraps the packet runner in one-child-at-a-time, resumable
  execution for slow model routes. It persists each episode result before applying an optional
  inter-request delay; it is useful for long GLM or other free-tier experiments and remains
  research-only.
- `audit_agenda_recovery.py` audits the raw items rejected by source revalidation. It tests exact
  full-source evidence recovery and formal/hierarchical reference recovery without accepting or
  rewriting anything; use it before changing the post-processor or rerunning the LLM.
- `build_agenda_recovery_shadow.py` applies the reusable shadow recovery layer to completed raw
  responses. It writes strict accepted items, separately marked recovered items with complete
  source evidence, and unresolved rejections; it never changes durable episode artifacts. Recovery
  can include a bounded forward expansion to a contiguous formal ID line (stopping at a blank
  line), so a trailing item ID remains available for transcript matching.
- `prepare_locator_recovery_review.py` keeps the original human crosswalk cases fixed while
  displaying strict plus shadow-recovered candidates in a fresh `RXR-*` packet. Use it to review
  recovery additions without changing the original packet's labels or sampling strata.
- `prepare_locator_crosswalk_review.py` selects a deterministic, development-only 48-chapter
  crosswalk packet (one case per episode, balanced across providers and review strata). It omits
  provider timings and includes the complete generated-candidate set plus source agenda lines.
  `serve_locator_crosswalk_review.py` and `locator_crosswalk_review.html` provide the localhost
  UI for labeling a matched candidate, consent/composite relationship, section/procedural chapter,
  missing candidate, source/extraction problem, or unsure case. Keep the packet and decisions under
  `/private/tmp`; these labels are scoring evidence, not production input.
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
