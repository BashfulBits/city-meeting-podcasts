# review/34 — LLM Quality Tournament & Cost-Gated Champion Routing

**Maturity: L3 (dev-ready) · breakout of [`review/27`](27-llm-backend-and-provider-routing.md) §6, generalized
across verbs and given its own doc 2026-07-17 · depends on R13 (shipped, [`review/33`](33-llm-quota-cost-scheduler.md))
· sibling design to [`review/35`](35-llm-confidence-calibration-human-review.md) · issues not yet cut · no code
exists yet**

> This is the **"A vs. B?" half** of this project's two-part LLM quality-assurance design. Its sibling,
> [`review/35`](35-llm-confidence-calibration-human-review.md), answers **"is this one candidate, at its own
> reported confidence, trustworthy?"** for verbs with a discrete, recurring label space (topic tags, future
> classification-style verbs) — implemented, shipped as part of R5. This doc answers a different question:
> **"which provider/model, in aggregate, produces better output for this verb?"** — a coarse, periodic,
> per-task-verb decision about which model gets to run in production at all, independent of any single
> candidate's own confidence score. Neither module substitutes for the other; a verb can use one, the other,
> or both. See §7 below for exactly how they compose.
>
> **Why this split exists (2026-07-17):** an earlier pass in this session concluded, incorrectly, that this
> tournament design already covered what review/35's module does, and removed that module on that basis. It
> was restored once the actual scope difference was worked through: this doc's tournament never inspects an
> individual candidate's confidence, and review/35's matrix never compares two providers against each other.
> They were always meant to coexist.

---

## §1. Problem & scope

Three providers are viable for this project's LLM verbs (`tag`, `summarize`, `soundbite-select`, and future
verbs) at meaningfully different price/quality/rate-limit points (`review/27` §2): Gemini (primary, free
tier), DeepSeek (secondary, cheap paid overflow), Mistral (tertiary, tight rate limit). Two questions follow
from having more than one viable provider:

1. **Is provider X currently better than provider Y at this verb?** Not per-candidate — in aggregate, across
   a sample of real inputs, judged by an independent third model.
2. **Given an answer to (1), should production traffic for this verb switch providers?** A cost-gated,
   human-approved decision, not an automatic one — the wrong provider silently taking over production output
   for an entire verb is a materially bigger blast radius than one bad tag suggestion.

This doc designs both: a weekly pairwise tournament (§2–§4) and a cost-gated champion-routing ticket (§5) that
consumes the tournament's results. Neither exists in code yet, anywhere in this repository — confirmed via
`git log --all -S` across every function/class name in this design, and a manual `grep` sweep of every remote
branch's code (not just docs), as of this doc's authoring pass.

## §2. Method — pairwise comparison, not absolute scoring

Research is unambiguous: "which is better, A or B" is far more reliable — for both human and LLM judges —
than "rate this 1–10." Maps directly onto two dimensions:

- **Factual accuracy** — judge sees the source transcript/agenda excerpt plus both candidates, asked which is
  more faithful, or whether both are equally faithful/unfaithful.
- **Style/quality** — judge sees both candidates, asked which reads better (length, tone, structure).

This is also why this mechanism, not a per-candidate score, is the right fit for **freeform generative
verbs** (`summarize`, soundbite selection) where review/35's ground-truth matrix structurally cannot apply
(§7) — there is no discrete "correct summary" to check a candidate against, but "is A's summary better than
B's" is a well-posed, judgeable question regardless.

## §3. The round-robin structure

With exactly 3 providers and the rule "a judge never grades its own family," round-robin covers all 3 pairs
with a genuinely independent judge every time, by construction — no configuration needed to avoid
self-preference bias:

| Contest | Candidates | Judge |
|---|---|---|
| 1 | DeepSeek vs. Gemini | Mistral |
| 2 | DeepSeek vs. Mistral | Gemini |
| 3 | Gemini vs. Mistral | DeepSeek |

Run per task-verb, on a **weekly cadence** (matching the stated 1–2/week human-time budget, and directly
mirroring H15's own periodic-calibration cadence — `review/11` §4 H15 row — same shape, different subject
matter). **Tool: Promptfoo** (MIT, SQLite-backed, no new infra service, already supports Gemini/DeepSeek/
Mistral natively) rather than a hand-rolled comparison harness — fits this project's "no extra
infrastructure" pattern better than building one from scratch.

**A design precedent already exists in this codebase for the blind-labeling half of this**, though not in a
form to import directly: `citypods/transcript_quality.py::_blind_mapping()` does exactly the "assign A/B
labels to exactly two candidates, hash-seeded order swap" mechanic this section needs — but it is private to
that module, hard-coded to exactly two candidates, and tightly coupled to ASR-specific comparison metrics
(`text_agreement`, `timing_delta_seconds`, `drift_badge`). It is a useful reference for the *pattern*, not
code to reuse as-is: a tournament module would need its own, LLM-verb-generic version of this idea.

## §4. Bias mitigation

Applied concretely, from research:

- **Run both orderings** (A-then-B and B-then-A) per judged pair to cancel position bias — the same pattern
  `_blind_mapping()` already applies for ASR comparisons (§3), generalized here to LLM verb output.
- **Decide tie-handling up front.** Recommend: ties count as half a win each, Elo-style, rather than being
  dropped — dropping ties silently shrinks the sample and can mask a genuinely-close result.
- **Validate the automated (Promptfoo/LLM-judge) result against the maintainer's own occasional human read**
  (the stated 1–2/week). If agreement is low, the automated judge isn't trustworthy yet for that verb, and
  champion decisions should lean on the human read until it is.

## §5. Cost-gated champion routing — the weekly ticket

**A challenger must clear a required win-rate margin over the current champion to be proposed as a switch**
— not just win more than half the judged comparisons. (Recommend a concrete default, e.g. >60% win rate over
a rolling sample, but this should be a tunable config value, not hardcoded.) If no challenger clears the
threshold for a verb that week, the ticket is FYI-only — no decision is requested, avoiding weekly decision
fatigue for a "nothing changed" week. **"Challenger" includes a within-provider tier upgrade** (e.g. DeepSeek
v4-flash → v4-pro, `review/27` §2.1), using the identical mechanism — the ticket doesn't need a separate code
path for "switch provider" vs. "switch tier within a provider," since both are just "propose a different
`model_id`" against the same recipe_hash-driven versioning.

**Weekly "champion stats" GitHub issue, one per task-verb** (settled 2026-07-17 — not one consolidated issue
with a section per verb; each verb's checkbox-decision lifecycle is independent, so a separate issue per verb
keeps one verb's decision/edit history from being interleaved with another's), following the existing
conventions this project already uses for recurring automated issues (`review/11` §4 H4 row: one
consolidated issue per check, a hidden JSON state block in the body for tracking — not an external ledger —
matching output), containing:

1. **Current quality results** — this week's (and recent rolling) win/loss/tie record per pair, for this
   verb.
2. **Cost implication per month** for each option — current champion vs. each challenger, using the real
   per-call cost the budget ledger already tracks (`review/33` §10.1, `citypods/compute/llm_budget.py`) —
   not an estimate, since ledger telemetry exists by the time this runs.
3. **One-time back-catalog recalculation cost** if switching — real per-call-cost telemetry means "cost to
   re-derive verb X across N already-processed episodes" is a simple multiplication from measured averages,
   not a guess.
4. **A checkbox decision block** (one checkbox per real option — keep current champion / switch to challenger
   X, with and without back-catalog upgrade) — modeled on the well-established "Renovate Dependency
   Dashboard" pattern (checkboxes in a bot-maintained issue that a scheduled Action parses and acts on),
   adapted to this project's own hidden-JSON-state-in-body convention rather than a new state-tracking
   mechanism.
5. **A separate Action, triggered on issue edit**, parses which box (if any) got checked, applies the routing
   config change (updates which provider is dispatched for that verb) and/or enqueues the back-catalog
   recalculation job if requested, then **clears the checkboxes** so the next week's ticket starts from a
   clean decision state.

## §6. Interfacing with the shipped R13 adapter

None of §2–§5 above are affected in *method* by what R13 (`review/33`) added underneath — pairwise judging,
the round-robin structure, and the weekly checkbox ticket are unaffected. What matters is the concrete call
shape a tournament runner uses against the actual shipped adapter:

- **Each judged comparison is its own `InferenceJob`/`LLMRequestPolicy` call**, one per candidate, with
  `allowed_models=(candidate_model,)` (an exact singleton — never the judge's model, which is a *separate*
  call with its own singleton) and `allow_paid=True`. This is the identical pattern R5/R12's own production
  dispatch already uses to pin to one model (`citypods/tags.py::llm_tag_suggestions`,
  `citypods/discovery/classify.py::classify`) — a tournament caller is not a special case of the interface,
  just a different caller of the same one:
  ```python
  from citypods.compute.llm_policy import LLMRequestPolicy

  job = InferenceJob(
      task="tag",  # or "summarize", "soundbite-select", any Task
      inputs={
          "messages": messages,
          "structured_output": CONTRACT_NAME,
          "llm_policy": LLMRequestPolicy(
              allowed_models=(candidate_model,),
              allow_paid=True,
              purpose="tournament:tag",
          ),
      },
      recipe_hash=recipe_hash,
  )
  result = backend.run_inference(job)
  ```
- **A tournament run should set a bounded `deadline_at`** (e.g. the remainder of that day's scheduled
  window) rather than leaving it unset: an unbounded policy is appropriate for R5's patient background
  tagging, but a weekly tournament run has a real cadence to keep — gate 5's off-peak preference and gate
  4's deadline gate (`review/33` §5) both key off this, so a candidate that would otherwise defer for
  DeepSeek's off-peak window can be told "don't wait past tonight's run."
- **A candidate contest can still come back as a deferred `JobHandle`** (quota exhausted, a 429, or Mistral
  genuinely in flight) exactly like any other caller — the tournament runner must tolerate a contest not
  completing within one invocation and either retry via the same registry lookup next run (`run_inference`
  called again with the same job) or explicitly `reconcile()` it, the same choice R5 faces (`review/14`'s
  note on this). Because each candidate's `recipe_hash` differs (it folds in `model_id`), concurrent contests
  for the same episode+verb never collide in the deferred registry or the ledger.
- **The tournament module needs its own structured-output contract registered wherever it calls
  `reconcile()`** — including inside the daily sweep (`scripts/llm_deferred_sweep.py`), if tournament
  requests are ever allowed to defer into it. A tournament contract that's only ever registered inside the
  tournament's own weekly-run process, and never in the sweep's process, would silently never benefit from
  the sweep's daily catch-up — the same failure mode found and fixed for R5's own contract registration
  (`review/14`'s 2026-07-17 R13-migration note).
- **Cost telemetry for §5 item 2 reads `RouteLedger.cost_used`/settled actuals from `state/llm_budget.json`**
  (`citypods/compute/llm_budget.py`) — the "real per-call cost the budget ledger already tracks" §5 already
  says, naming the actual object.
- **The allowlist means a tournament call can never silently consume production's free-route budget beyond
  what it's explicitly pinned to** — each contestant's own singleton allowlist is the isolation mechanism,
  not a separate quota carve-out this doc needs to invent.

## §7. Extensibility across verbs, and composing with review/35

This design must work identically for `tag`, `summarize`, `soundbite-select`, and any future verb — per this
project's standing "no re-architecture to add a verb" requirement (`review/27` §3.1). Nothing in §2–§6 above
is tag-specific: `Task` is a free-form string, `TASK_VERSIONS`/`TASK_PROMPTS` are already keyed by `Task`, and
`LLMRequestPolicy.purpose` is just a telemetry string — `"tournament:summarize"` works exactly like
`"tournament:tag"`.

**How this composes with review/35's per-candidate ground-truth matrix, concretely:**

- A verb with a discrete, recurring label space (topic tags today; a future classification-style verb) can
  use **both**: review/35's matrix decides whether an *individual* candidate from the currently-pinned model
  is trustworthy enough to admit; this doc's tournament periodically decides *which model* should be pinned
  at all. They operate on different axes and don't need to agree or coordinate — a champion-routing switch
  (§5 item 5) just updates the config value review/35's matrix keys `provider_model` off of (e.g.
  `config/site_config.yml`'s `tagging.llm_model`); the matrix itself re-accumulates review evidence for the
  new route from scratch, same as it would for any other route change.
- A verb with no discrete recurring label (freeform `summarize`, soundbite selection) has **no ground-truth
  matrix to build at all** — review/35's mechanism structurally doesn't apply (review/35 §8). This doc's
  tournament is the *only* quality-assurance mechanism available to such a verb, not a complement to a
  matrix that can't exist for it.
- Neither module ever gates on the other's state. A tournament contest for `tag` doesn't check review/35's
  matrix, and review/35's admission check doesn't consult tournament results. They're independent by design
  (§ intro above) — this is deliberate, not an integration gap to close later.

## §8. Data model

1. **Champion-routing config** — which provider is currently dispatched for each task-verb; updated only by
   the weekly-ticket checkbox flow (§5), never silently. Concretely, this is each feature's own
   `LLMRequestPolicy(allowed_models=(...))` construction site — for `tag`, `config/site_config.yml`'s
   `tagging.llm_model`, read into the singleton allowlist `citypods/tags.py::llm_tag_suggestions` pins to. A
   champion-routing switch updates that config value; no separate "which provider is current" state needs
   inventing beyond what each feature already reads.
2. **Shadow/comparison outputs** — stored separately from any canonical/served value, keyed by
   `(task, provider, recipe_hash)`, never overwriting a canonical field. **Exact storage location (record
   dict vs. a dedicated CAS block) is an open implementation question, left deliberately unresolved here** —
   confirmed with the maintainer (2026-07-17) that this is fine to settle at implementation time rather than
   design time. Leaning toward a dedicated block (mirrors how `speakers`/diarization output gets its own
   `ARTIFACT_BLOCKS` entry, `citypods/records.py` §"Extensibility" comment) so tournament data doesn't bloat
   the primary record read/write path, but this is not settled. **Not provided by R13** — its ledger/registry
   are explicitly scoped to never track *which* jobs are pending, only route capacity (`review/33` §10.5) —
   this remains this module's own storage to design and build.
3. **Async dispatch/deferred state** — fully covered by R13's shipped deferred-request registry
   (`citypods/compute/llm_deferred.py`, `state/llm_deferred/*.json`); nothing new needed here (§6).

## §9. Module / file plan

- **New tournament/champion-routing module** (e.g. `citypods/tournament.py` or similar — naming not yet
  settled) — sits *beside* `citypods/compute/llm_policy.py`/`llm.py`, importing them the same way any
  feature Stage does (§6), not modifying either.
- **New weekly-ticket GitHub Actions workflow** (`.github/workflows/`, matching
  `availability-digest.yml`'s cadence/structure precedent) + its companion checkbox-parsing Action (§5 item
  5).
- **A generalized version of `_blind_mapping()`'s pattern** (§3) — order-randomization + blind labeling for
  exactly two LLM-verb candidates, independent of `transcript_quality.py`'s ASR-specific metrics.

## §10. Tests

- Round-robin contest generation: given 3 providers, exactly 3 contests, judge never matches either
  candidate's family, deterministic given a fixed provider list.
- Order-swap bias mitigation: the same pair judged both orderings, results combined correctly (including tie
  handling as half-wins).
- Win-rate margin gate: a challenger below the configured margin never triggers a checkbox proposal; the
  ticket is FYI-only that week.
- Champion-routing Action: a checked box updates the correct config value and clears all checkboxes; an
  unchecked ticket changes nothing.
- `LLMRequestPolicy` construction: singleton `allowed_models`, `allow_paid=True`, bounded `deadline_at`, and
  a tournament-specific `purpose` string, verified against a real `InferenceJob` call site.
- Deferred-handle tolerance: a contest that comes back as a `JobHandle` is retried or reconciled correctly,
  never treated as a contest loss.

## §11. Risks

- **The automated LLM judge may not be reliable for every verb from day one** — §4's human-validation check
  exists specifically to catch this; champion decisions should lean on the human read until agreement is
  established, not assume the judge is trustworthy by default.
- **A verb with no discrete label space (summarize, soundbite-select) has no fallback quality mechanism
  besides this tournament** (§7) — if the tournament is delayed or under-resourced, those verbs ship with
  *no* quality-assurance mechanism at all, unlike `tag`, which at minimum has rule tags as an always-on
  baseline.
- **Cost-gated margin tuning is a real judgment call** — too low a margin causes provider churn on noisy
  samples; too high a margin means a genuinely-better challenger never gets proposed. Should be a tunable
  config value, revisited once real tournament data exists, not hand-picked once and left static.

## §12. Sequencing & dependencies

Depends on R13 (`review/33`, shipped) for the request/quota/deferred-completion plumbing (§6). Does **not**
depend on review/35's calibration matrix — they're independent siblings (§7), and this module can ship for a
verb (e.g. `summarize`) that never uses review/35's mechanism at all. First real consumers are expected to be
whichever verbs ship next with more than one viable provider — currently `tag` (`review/14`) and the R6 bundle
(`review/30`, cards/summaries/soundbites), per each of those docs' own cross-references to this one.

## §13. Acceptance

A weekly, per-task-verb tournament runs pairwise contests across all three providers with an independent
judge every time; results are order-swap bias-mitigated and validated against an occasional human read; a
challenger must clear a configured win-rate margin before a champion-routing switch is even proposed; the
weekly ticket reports quality, live per-month cost, and one-time back-catalog cost, and a checked box applies
the switch (and clears itself) via an Action, never automatically or silently. The design interfaces with
R13's shipped adapter using the identical `LLMRequestPolicy` pattern every other caller uses, and composes
with, rather than duplicates, review/35's ground-truth calibration matrix.

## Proposed GitHub issues (not filed — batch review pending)

1. Tournament: Promptfoo-based round-robin pairwise comparison, weekly cadence, bias mitigation (order-swap,
   tie policy, human-calibration check).
2. Weekly champion-stats GitHub issue + checkbox-approval Action (quality, cost, back-catalog-cost,
   apply-and-clear).
