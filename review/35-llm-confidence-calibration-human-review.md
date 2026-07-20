# review/35 — Reusable LLM Confidence Calibration & Human Review

**Maturity: Implemented (shipped as part of R5, `citypods/llm_evaluation.py`) · design write-up added
2026-07-17, extracted and generalized from [`review/14`](14-topic-tags-strong-towns-lens.md)'s inline
description · currently wired only to R5's tagging config — see §9 for the open generalization gap ·
sibling design to [`review/34`](34-llm-quality-tournament-champion-routing.md)**

> This is the **"A: true or false?" half** of this project's two-part LLM quality-assurance design. Its
> sibling, [`review/34`](34-llm-quality-tournament-champion-routing.md), answers **"which provider/model, in
> aggregate, is better?"** — a coarse, periodic, per-verb decision, unbuilt as of this writing. This module
> answers a narrower, already-implemented question: **"is this one specific candidate, at its own reported
> confidence, trustworthy enough to show?"** — self-calibrating from real human ground-truth review of real
> production candidates, continuously, not on a fixed weekly cadence. See §8 for exactly which verbs this
> approach can and can't apply to, and §"composing with review/34" there for how the two fit together.
>
> **Why this write-up exists (2026-07-17):** the module was briefly removed in this session under the
> mistaken belief that review/34's (then-unbuilt) tournament design already covered its function. It does
> not — review/34 never inspects an individual candidate's confidence, and this module never compares two
> providers against each other. The module was restored, and this doc captures its design properly, since it
> had previously only ever been described inline inside `review/14` (R5-specific), despite being written as
> feature-independent code from the start.

---

## §1. Problem & scope

An LLM feature (R5 topic tags today) can generate additive, evidence-backed candidate output from day one,
before any of it has been human-verified. The problem: how does a candidate's own **reported** confidence
score become **trustworthy** — i.e., how do we know a model's "0.8" actually means ~80% precision in
practice, for *this* specific label, from *this* specific model, at *this* prompt/taxonomy version? A wrong
answer in either direction is bad: admitting everything from day one publishes unverified model output as
if it were fact; requiring 100% precision before ever admitting anything makes the LLM-assist path
permanently invisible.

The answer implemented here: **collect real production candidates from day one, route a prioritized sample
to a human reviewer weekly, and let an evolving, per-exact-key admission threshold emerge from that real
ground truth** — rather than hand-picking a global confidence cutoff up front. A specific tag suggestion
becomes visible not because a maintainer manually decided "0.7 is probably fine," but because enough human
reviews of *that exact* (feature, provider/model, tag, scope) combination showed that a 0.7-or-above
suggestion is correct at least 90% of the time (`EvaluationConfig.required_precision`, lowered from an
initial 95%, §13).

## §2. The matrix design — exact keys, sparse rows

`candidate_matrix_key()` (`citypods/llm_evaluation.py:104`) derives the exact calibration dimensions for any
candidate:

```python
{
    "feature": ...,          # e.g. "topic-tags" — which feature/verb this candidate belongs to
    "provider_model": ...,   # e.g. "litellm:gemini/gemini-3-flash-preview" — exact route
    "prompt_version": ...,   # this feature's own prompt/schema version
    "taxonomy_version": ..., # this feature's own content-version (taxonomy, classification schema, ...)
    "label": ...,            # the specific tag ID / classification label this candidate proposes
    "scope": ...,             # "chapter" or "episode" — R5's own scope; other features may use a
                              #   different discrete scope value, or omit the distinction
}
```

Every dimension matters: a route change (different model), a prompt change, or a taxonomy/schema version
bump should each start a *fresh* calibration for that exact combination rather than silently reusing
evidence collected under different conditions. The **label** dimension is what makes this a *sparse* matrix,
not a single global threshold — different tags/labels are trusted at different confidence levels even from
the same model (e.g. a model may be very reliable at spotting `parking-mandates` but noisier at
`neighborhood-engagement`), and this design lets each cell calibrate independently rather than forcing one
number to fit every label.

**This sparseness is also the source of this design's core limitation — see §8.**

## §3. Admission — resolving a threshold, then applying it

`resolve_threshold()` (`:138`) looks for an exact, **qualified** matrix row for a candidate's key; if none
exists, it falls back to `EvaluationConfig.fallback_for(feature, provider_model)` — a per-feature,
per-route configured fallback, defaulting to `1.0` (`fallback_confidence`) when no more specific fallback is
configured. `1.0` as a default is deliberate: since reported confidence is clamped to `<= 1.0`, an
unreviewed row's fallback is **effectively unreachable**, so shadow candidates accumulate without ever
becoming visible until either the row earns a real calibrated threshold, or a maintainer deliberately lowers
an unquantified fallback as an explicit policy decision.

`apply_admission()` (`:151`) then decides admission:

```python
admitted = confidence > threshold if basis == "fallback" else confidence >= threshold
```

The asymmetry here is deliberate, and was a real correctness bug fixed in this branch's review pass: a
**qualified** threshold is itself an observed confidence value from real human review, so meeting it
*exactly* must admit (`>=`). An unreviewed **fallback** threshold carries no such evidence; requiring the
candidate to strictly *exceed* it keeps a fallback of `1.0` truly unreachable — a model reporting exactly
`1.0` confidence with zero real calibration behind it must not slip through on a `>=` comparison against its
own maximum possible value.

## §4. Matrix refresh — deriving thresholds from human review

`refresh_matrix()` (`:189`) recomputes every sparse row from scratch, from the full set of recorded human
decisions (`REVIEW_DECISIONS = ("correct", "incorrect", "ambiguous")`), grouped by exact matrix key:

1. Collect every distinct observed confidence value for this key's reviews, sorted ascending — each becomes
   a **candidate threshold**.
2. For each candidate threshold (lowest first), compute precision among reviews with confidence **at or
   above** it: `correct / total`.
3. The **first** (lowest) threshold that has both `>= minimum_reviews` samples and `>= required_precision`
   precision **qualifies** — that becomes the row's admission threshold.
4. If no threshold qualifies, the row stays unqualified and falls back to the feature/route default (§3).

Choosing the **lowest** qualifying threshold, rather than the highest-precision one, is the point: it
maximizes the fraction of future candidates that get admitted while still meeting the precision bar — a
higher threshold would be more conservative than necessary once the bar is already cleared. Each refresh
also appends a **trend point** (`rows`, `qualified_rows`, `reviewed` counts, timestamped) to a capped
52-entry rolling log, deduplicated so unrelated `refresh_matrix()` calls (which stamp a fresh `updated_at`
regardless of whether anything actually changed) don't spam the trend history — see §"policy_fingerprint"
below for the related fix this required.

**`policy_fingerprint()`** (`:470`) hashes only the parts of the matrix that actually change an admission
decision (`key`, `qualified`, `threshold` per row) plus the config, deliberately excluding each row's
`updated_at` timestamp — otherwise every ingested review would invalidate every episode's cached
tag-projection hash, even when no actual admission decision moved. This fingerprint feeds a calling
feature's own recipe-hash computation (`citypods/tags.py::tag_recipe_hash`'s `admission_policy` parameter)
so a genuine calibration change re-projects cached candidates without re-calling the model, while an
irrelevant timestamp bump does not.

## §5. State & persistence

`load_state()`/`save_state()` (`:81`, `:98`) read/write a single JSON object (default path
`llm_evaluation.json`, feature-configurable via `EvaluationConfig.state_path`):

```json
{
  "version": 1,
  "reviews": { "<candidate_id>": { "...": "one immutable-by-id human decision record" } },
  "matrix": [ { "key": {...}, "matrix_id": "...", "reviewed": 12, "qualified": false, "threshold": null, ... } ],
  "trend": [ { "at": "...", "rows": 4, "qualified_rows": 1, "reviewed": 42 } ]
}
```

`reviews` is keyed by `candidate_id` (`:122`) — a stable hash over the candidate's matrix key plus
`episode_uid`/`chapter_id`/`recipe_hash`/`confidence`, so the *same* candidate reviewed twice (e.g. a
re-opened issue edit) overwrites its prior decision rather than double-counting it. `matrix` is fully
derived (§4) — safe to recompute from `reviews` at any time, never hand-edited. `trend` is a capped rolling
log for the weekly digest's own historical context.

## §6. Human review workflow

**Selection** (`select_review_candidates()`/`review_priority()`, `:296`/`:313`): every unreviewed candidate
(by `candidate_id`) is sorted by priority — unqualified rows first, then rows with zero reviews, then fewer
reviews, then **closest to the current threshold** (the boundary case most informative to a human reviewer),
then a stable tiebreak — and the top `review_batch_size` (default 20) are selected for this week's digest.

**Digest + child issues** (`render_digest()`/`render_review_body()`, `:500`/`:345`): a weekly parent issue
shows the full sparse matrix (reviewed count, precision, threshold, qualified/sparse status per row) plus a
checklist of this week's selected review candidates; each selected candidate gets its own **native GitHub
sub-issue** with
the model's explanation (blockquoted — see §7), bounded evidence (quoted transcript span with derived
timestamp, or an allowlisted document link/locator), and a three-way checkbox (`Correct` / `Incorrect` /
`Ambiguous`).

**Ingest** (`ingest_review_body()`/`parse_review()`, `:444`/`:422`): a scheduled/comment-triggered workflow
parses a checked box plus the embedded metadata marker back into a `record_review()` call, then re-runs
`refresh_matrix()` so newly-qualified rows take effect on the next normal build — no second LLM call needed.
Once every native child in a batch is resolved, the ingest workflow closes its parent; the next weekly
digest opens a fresh parent, keeping each batch bounded below GitHub's 100-sub-issue limit.

**Wired today as** `.github/workflows/llm-tag-review.yml` (weekly digest packaging + issue open/update) and
`llm-tag-review-ingest.yml` (scheduled + comment-triggered ingestion), both R5-specific in name and trigger
condition (see §9).

## §7. Security hardening (found and fixed in this branch's review pass)

- **`render_review_body()` blockquotes untrusted model text** (`explanation`, evidence `quote`) via
  `_quote_block()` — an unquoted line from a model's own explanation could otherwise read as a literal
  `- [x] Correct` line and be visually mistaken for a real decision checkbox.
- **`parse_review()` takes the *last* metadata marker match**, not the first — untrusted candidate text
  rendered earlier in the body (explanation, document_locator) could otherwise be crafted to contain a
  marker-shaped decoy before the genuine one `render_review_body()` always appends last.
- **`_safe_link()` reuses the project's shared SSRF gate** (`citypods.security.validate_source_url`,
  `resolve=False` — the same offline/fast mode used at config-load time) before rendering any evidence
  document link, rather than trusting a model-supplied URL directly.
- **The comment-triggered ingest workflow requires collaborator-or-above `author_association`** plus a
  matching issue title, so an arbitrary public commenter cannot fabricate a calibration review by commenting
  `/llm-ingest` on an unrelated issue.
- **The ingest workflow's matrix-mutating jobs are serialized** (`max-parallel: 1`) so concurrent runs cannot
  clobber each other's recorded review decisions in the shared state file.

**One related gap remains deliberately open:** `ingest_review_body()` still trusts the candidate JSON
embedded in an *edited* review issue verbatim, with no cross-check against a durable, feature-owned
candidate ledger. Closing this needs the module to gain a lookup capability into feature-specific storage —
a real design addition, not a bolt-on, and out of scope for this write-up.

## §8. Structural limitation — requires a discrete, recurring label

**This mechanism only works for verbs with a discrete, recurring label space.** The matrix key's `label`
dimension (§2) must recur across many episodes for evidence to accumulate against it — exactly what a
taxonomy tag ID (`parking-mandates`, tagged across hundreds of episodes) provides, and exactly what a future
classification-style verb (e.g. "is this a public hearing?") would also provide.

**A freeform generative verb has no such recurring label.** `summarize`'s output is unique text per episode
— there is nothing to accumulate the required 12 reviews *against* the same "label," because there is no
label at all, only unbounded text. Flattening the matrix to one row per route (dropping the `label` dimension, treating
the whole verb as a single constant "label") is *technically* possible with this schema, but it is a much
coarser tool than what's built, is not what the current implementation does, and was not designed for.

**Composing with review/34's tournament:** for a discrete-label verb, this module and review/34's tournament
operate on genuinely different axes and can run simultaneously without coordinating — this module decides
whether an individual candidate from the *currently pinned* model is trustworthy; review/34 periodically
decides *which model* should be pinned at all (`review/34` §7). For a freeform verb, this module's mechanism
does not apply at all — review/34's pairwise tournament is the *only* available quality-assurance mechanism,
not a complement to a matrix that structurally can't exist for that verb.

## §9. Current wiring gap — generic in design, R5-specific in practice

The module itself (`citypods/llm_evaluation.py`) takes no dependency on tags, taxonomies, or R5 — every
function operates on a plain `candidate: dict[str, Any]` with the keys §2 describes. In practice, however,
every integration point around it is still R5-specific, despite the module's own docstring stating "future
`summarize` and `soundbite-select` features can use the same module":

- `StageContext`'s `llm_evaluation_state_path`/`llm_evaluation_config` fields (`citypods/stages.py`) are
  populated only from `config/site_config.yml`'s `tagging.evaluation.*` block (`citypods/run.py`) — a
  second feature would need its own equivalent `StageContext` fields and its own config-reading code,
  duplicating this wiring rather than sharing it.
- The `citypods llm-evaluation` CLI subcommand (`citypods/cli.py`) dispatches unconditionally to
  `citypods/llm_tag_review.py` — a **tag-specific** packaging module (its digest/issue titles literally say
  "R5 LLM tag calibration" / "R5 LLM tag sample `<id>`"). A second feature could not reuse this CLI entry
  point or these workflows without either forking them or generalizing the title/routing logic first.
- `.github/workflows/llm-tag-review.yml`/`llm-tag-review-ingest.yml` are themselves R5-named and R5-scoped
  (issue titles, the `citypods llm-evaluation package` invocation).

**This is the next open item** (flagged by the maintainer, 2026-07-17, to be picked up after this doc):
aligning the calling convention so a second LLM-assisted feature (most likely R6's `summarize`-adjacent
classification needs, or a future discrete-label verb) can register its own candidate source, config block,
and review-issue naming without duplicating `StageContext`/CLI/workflow plumbing per feature. This is
separate from, and should be sequenced alongside, re-aligning this module's *callers* (`citypods/tags.py`)
to R13's current adapter interface (`review/33`) — the calibration matrix itself has no direct dependency on
the LLM adapter (it operates purely on already-validated candidate dicts), but `decorate_llm_candidates()`'s
`provider_model` field must keep reflecting whatever R13's scheduler actually resolved, not a precomputed
value, for the matrix key to stay meaningful.

## §10. Data model

- **`Episode.llm_tag_candidates`** (R5-specific consumer, `review/14` §"Data model deltas" #4) — the
  feature-side candidate storage this module reads from and decorates; not owned by this module.
- **Review record** (`record_review()`, `:260`) — `candidate_id`, full matrix key, `confidence`, `decision`,
  episode/chapter identity, `reviewed_at`/`reviewed_by`, and the originating issue number/URL for audit.
- **Matrix row** (`refresh_matrix()`, §4) — `key`, `matrix_id`, `reviewed`/`correct`/`incorrect`/`ambiguous`
  counts, `qualified`, `threshold`, `precision`, `qualified_count`, `updated_at`.

## §11. Module / file plan

- `citypods/llm_evaluation.py` — implemented, feature-independent (§1–§7).
- `citypods/llm_tag_review.py` — implemented, but R5-specific (§9) — the concrete thing a generalization pass
  would need to either parameterize or replace with a per-feature equivalent.
- `.github/workflows/llm-tag-review.yml` / `llm-tag-review-ingest.yml` — implemented, R5-named (§9).
- `citypods/cli.py`'s `llm-evaluation` subcommand — implemented, hardwired to the R5 script (§9).

## §12. Tests

`tests/test_llm_evaluation.py` covers: the `1.0` fallback keeping unquantified candidates shadow-only; the
strict-exceed-for-fallback vs. `>=`-for-qualified admission asymmetry (§3); sparse exact-matrix qualification
requiring both `minimum_reviews` and `required_precision`; `policy_fingerprint()` ignoring matrix-row
timestamp churn; automatic admission after a human review qualifies a row; evidence-rich issue
parsing/rendering, including the last-marker-match and blockquoting security fixes (§7); and that untrusted
candidate text cannot forge a review decision or spoof the review marker.

## §13. Risks

- **Cold start is genuinely slow by design.** A new (feature, route, label, scope) combination starts fully
  shadow (fallback `1.0`) and needs `minimum_reviews` (default 12, lowered from an initial 30 on 2026-07-17
  — with `required_precision` correspondingly lowered from 95% to 90%, so the precision bar stays
  meaningful at the smaller sample size) real human reviews before it can ever qualify — for a taxonomy with
  dozens of labels, filling in the whole matrix still takes real time at a 20/week review batch size. This
  is an intentional tradeoff (trustworthy-but-slow over fast-but-unverified), not an
  oversight, but it means a feature adopting this module should expect a long shadow period before LLM
  output becomes broadly visible.
- **Does not generalize to freeform generative verbs** (§8) — a feature relying solely on this module for a
  freeform verb would have no quality-assurance mechanism at all; it must use review/34's tournament instead
  (or in addition, for discrete-label verbs).
- **The wiring gap (§9)** means every additional feature currently re-derives its own `StageContext`/CLI/
  workflow integration rather than sharing one — a growing maintenance cost the longer generalization is
  deferred.

## §14. Sequencing & dependencies

Implemented as part of R5 (`review/14`); no dependency on R13 beyond its consuming feature's own dispatch
call (this module never calls an LLM itself — it operates purely on already-validated candidate dicts a
feature hands it). Independent of review/34 (§8's "composing" note) — either, both, or neither can be used
per verb. The wiring generalization (§9) is the prerequisite for any second feature (most plausibly a
future discrete-label classification verb) adopting this module without duplicating its integration code.

## §15. Acceptance

A feature can record additive, evidence-backed LLM candidates from day one without any of them becoming
visible until real human-reviewed evidence justifies it; admission thresholds emerge per exact
(feature, route, label, scope) combination from real ground truth, not a hand-picked global cutoff; a
weekly, evidence-rich review digest and per-candidate child issues let a maintainer spend a bounded 1–2
hours/week driving calibration; ingested decisions take effect on the next normal build with no additional
LLM call; and the whole mechanism is provably feature-independent in its core module, even though its
current wiring (§9) has not yet been exercised by a second feature.
