# review/27 — LLM Backend & Provider Routing (Phase R)

**Maturity: L3 · authored 2026-07-14 · breakout of [`review/11`](11-technical-design-roadmap.md) Phase R
· ROADMAP R2 (LLM backend) + R10 (rate-limited dispatch Worker, numbered out of table-position on
purpose — see ROADMAP's insert note) · §1–§3/§9 (the adapter + Worker) shipped via R2/R10; the former §6
(quality tournament/champion routing) was broken out into its own doc,
[`review/34`](34-llm-quality-tournament-champion-routing.md), 2026-07-17 — still proposed, issues not yet
cut**

> This is the first real adapter for the H13-reserved `tag`/`summarize`/`soundbite-select` compute verbs.
> R5 (topic tags, LLM-assist path) and R6 (auto-summaries, soundbite selection) are its first feature
> consumers; neither builds this under its own time pressure. Design decided across a planning
> conversation with the maintainer on 2026-07-14; every provider/pricing/rate-limit figure below was
> verified via web research on that date and should be re-checked before implementation, since these
> figures move.

**2026-07-17 revision note (this pass corrects §3/§5/§5.2/§7/§8/§10/§11 only — verified against R5's
actual migration, not just re-read):** R2's adapter (`citypods/compute/llm.py`) and R10's Worker shipped
as this doc describes. What has *also* since shipped, but wasn't anticipated by this doc's original
text, is R13 (`review/33`) — a policy/scheduler/budget layer between a calling Stage and the adapter that
this doc's §5/§8 originally sketched as future work under the H14d-ledger/H5-ordering analogy. R13's
`LLMRequestPolicy`/`select_and_reserve`/`LLMBudget` *are* that ledger and allocation machinery, now real
code, not a parallel thing to build.

**The former §6 (quality tournament & cost-gated champion routing) was broken out into its own doc,
[`review/34`](34-llm-quality-tournament-champion-routing.md), later the same day** — it grew a sibling
design ([`review/35`](35-llm-confidence-calibration-human-review.md), the per-candidate ground-truth
calibration module used by R5's tagging) that needed its own full write-up, and the two are easiest to
read and maintain side by side rather than one buried as a subsection here. The tournament itself is
still unbuilt (confirmed: no tournament/champion/Promptfoo code exists anywhere in this repository as of
this pass) — see review/34 for the full design, including how it interfaces with R13's shipped adapter.
`classify-civic-platforms` (added later by R12/review/28, not anticipated by this doc's original verb
list) is a classification task judged on accuracy, not a generative "which reads better" comparison — out
of the tournament's scope by design, not an oversight; R12 already gives it its own single-free-route
policy.

---

## §1. Problem & scope

The H13 compute-backend interface (`citypods/compute/base.py`, shipped, pre-1.0-locked) already types a
`Task` `Literal["transcribe", "align", "diarize", "summarize", "tag", "soundbite-select"]` and the
`InferenceJob(task, inputs, recipe_hash)` / `Backend.run_inference(...)` contract. No adapter implements
the LLM-facing verbs (`summarize`/`tag`/`soundbite-select`) yet — only the ASR-facing `local` adapter
exists. This item builds that adapter, plus the provider-allocation, versioning, budget, and
quality-calibration machinery around it, so R5/R6 (and later `embed`/`translate`/`extract`, already
reserved in review/11 §3.5) consume a working, tested system rather than inventing one each.

**Explicitly in scope:** the LLM `Backend` adapter; three-provider routing (Gemini, DeepSeek, Mistral);
retrieval-scoped chunking; per-task-verb versioning; a soft budget ledger; a quality tournament with
cost-gated champion routing; the rate-limited dispatch Worker (R10) that makes tightly-limited providers
usable from a scheduled pipeline. **Explicitly out of scope:** any specific LLM feature (tagging,
summarization) — those are R5/R6's own designs, consumers of this interface, not built here.

---

## §2. Provider strategy — corrected pricing/tier facts (verified 2026-07-14)

| Provider | Free tier shape | Relevant model | Rate limits | Context window | Role here |
|---|---|---|---|---|---|
| **Gemini** | **Ongoing**, not a trial | Gemini 3 Flash | 10 RPM / 250K TPM / 1,500 RPD | 1M tokens | **Primary production channel** |
| **DeepSeek** | One-time 5M-token grant, 30-day expiry, then pay-per-token | **deepseek-v4-flash** (default) / **v4-pro** (cost-gated upgrade) | Concurrency-based, not RPM/RPD | 1M tokens (both) | **Secondary — cheap paid overflow + tournament participant** |
| **Mistral** | **Ongoing** "Experiment" tier, ~1B tokens/month | Mistral Large 3 | ~2 RPM (plan for 1/min) | 256K tokens | **Tournament judge + occasional per-verb champion, not a default full-catalog channel** |

**Corrections from the initial framing:** DeepSeek's "free allotment" is a one-time 30-day trial, not an
ongoing allowance — its actual advantage was being extremely cheap once paid (pre-cutover
deepseek-v4-flash: $0.14/M input, $0.28/M output), not free. Gemini's free tier is the one that's genuinely
ongoing, which is why it's the primary channel below, not DeepSeek. `deepseek-chat`/`deepseek-reasoner`
are deprecated 2026-07-24 in favor of `deepseek-v4-flash`/`deepseek-v4-pro` — use the new names.

### §2.1 DeepSeek v4-flash vs. v4-pro — a within-provider tier decision, not just a model string

**Added 2026-07-15 — v4-flash is a planned, distinctly-tracked option, not just "available because
LiteLLM lets you pass any model string."** v4-flash is consistently **~3x cheaper than v4-pro** across
every dimension ($0.14 vs $0.435/M cache-miss input, $0.28 vs $0.87/M output) while retaining the
**same 1M-token context window** — the large-context benefit isn't traded away for the cost savings.
Notably, per DeepSeek's own deprecation note, `deepseek-chat`/`deepseek-reasoner` (the old
non-thinking/thinking mode names) both now map onto v4-flash, not just the "fast/simple" of the two — so
v4-flash isn't a stripped-down model in the way "flash" tiers sometimes are elsewhere, it's more capable
than the naming alone suggests.

**Default DeepSeek to v4-flash; v4-pro is a cost-gated upgrade, evaluated per task-verb — reusing the
exact champion-routing mechanism from `review/34`, not a new one.** This mirrors the provider-level policy
precisely: v4-flash is the cheap baseline (analogous to Gemini's free tier at the provider level),
v4-pro must clear the same required win-rate margin (review/34 §5) to be proposed as the DeepSeek-side tier for
a given verb — e.g. "may do fine for simpler summarization tasks" on flash, while a verb that needs more
reasoning capability (potentially `tag`, which has to weigh nuanced taxonomy judgment) might justify the
upgrade. **No new versioning machinery needed:** `recipe_hash` already folds in `model_id` (§7), so
switching a verb between `deepseek/deepseek-v4-flash` and `deepseek/deepseek-v4-pro` is already a normal
version bump through the existing mechanism — this is the existing design generalizing cleanly, not a
gap. **Judging note:** a flash-vs-pro comparison for a given verb still needs a non-DeepSeek judge
(Gemini or Mistral) per the same self-family-bias rule as the provider-level tournament, since both
candidates share the DeepSeek family.

**Why three providers, not two:** the round-robin tournament (review/34) requires exactly this — with 3 models
and the rule "a judge can never grade its own family," a round robin covers all 3 pairs with zero
self-judging, by construction. Two providers can't do this (whichever one isn't being compared has to
judge, which is fine for exactly one pair but leaves the other pair with no independent judge).

---

## §3. Architecture — LiteLLM adapter, our own async dispatch transport beside it

**Decision: adopt LiteLLM plus Instructor/Pydantic for structured output**, not hand-rolled
per-provider clients or per-task JSON parsing. LiteLLM remains the provider-routing boundary;
Instructor turns a task-owned Pydantic response model into the appropriate provider request, validates
the returned object, and provides bounded validation-feedback retries for direct work. Unlike the
Pagefind-vs-MiniSearch call in `review/13`, this is a pure-Python pip dependency (no new build toolchain)
that already speaks Gemini, DeepSeek, Mistral, and effectively every other provider likely to matter
later — "adopt to other providers" (the requirement driving this whole design) becomes near-zero marginal
code per new provider, which a hand-rolled adapter can't match. Needs to clear `review/22`'s
dependency-pinning policy like any new dependency.

- `citypods/compute/llm.py` — new. A `Backend`-conforming adapter (`citypods/compute/base.py`'s
  `Backend` Protocol) that builds one normalized chat request from `job.task`/`job.inputs` and has two
  execution paths:
  - **direct**: call LiteLLM through Instructor with the task's Pydantic response model, then map the
    validated raw completion to a `JobResult`;
  - **rate-limited**: submit that same normalized request to the R10 Worker and return a `JobHandle`.
    A later reconcile/poll operation fetches the Worker result, validates it against the same Pydantic
    model, and maps it to the same `JobResult` shape.
    The H13 union return type is intentional: a direct LiteLLM call completes in-band, while the Worker
    route preserves the no-runner-idle queue boundary.
- Instructor owns structured-output mode selection, Pydantic validation, and the one bounded direct
  corrective retry. LiteLLM owns provider-native wire-format translation and response normalization.
  **Neither owns provider selection or this project's durable pacing** —
  those depend on our budget/tournament state and the explicit rate-limit policy in §5/§9.
- The R10 endpoint is **OpenAI-shaped asynchronous transport, not a synchronous LiteLLM provider
  endpoint**: an initial `POST /v1/chat/completions` returns `202` + a poll location; an idempotent
  re-submit may return that request's terminal `200` response. Therefore `llm.py` must use its
  enqueue/poll protocol (and consume the terminal idempotent response) rather than passing the Worker URL
  as `api_base` to a plain
  `litellm.completion()` call. This keeps LiteLLM as the provider adapter without pretending an async
  queue is a synchronous completion API.
- A Worker deployment may point at (a) a provider's own OpenAI-compatible endpoint, such as Mistral's,
  or (b) an OpenAI-compatible LiteLLM Proxy when the selected provider needs native wire-format
  translation. The Cloudflare Worker never grows provider-specific clients; the latter proxy is a
  separately provisioned LiteLLM runtime and is not hidden inside the JavaScript Worker.
- Model strings follow LiteLLM's `provider/model` convention: `gemini/gemini-3-flash-preview`,
  `deepseek/deepseek-v4-flash`, `deepseek/deepseek-v4-pro`, `mistral/mistral-large-latest`
  (with `mistral/mistral-large-3` retained for compatibility).

### §3.1 Extensible task-verb design (per maintainer requirement — no re-architecture to add a verb)

Keep `Task` as a `Literal`, don't loosen it to `str`. It's cheap to extend (one line) and it's the entire
point of H13's "typed for the full verb set up front" pre-1.0 lock — loosening it would undo that
deliberate design decision, not simplify it. The actual extensibility requirement is that everything
**downstream** of the Literal must be registry/dict-keyed by task name, never a chain of
`if task == "tag": ... elif task == "summarize": ...` branches:

- `TASK_VERSIONS: dict[Task, str]` (§7) — one entry per verb.
- `TASK_PROMPTS: dict[Task, PromptTemplate]` (§4.3) — one entry per verb.
- The budget ledger (§8) and tournament (review/34) already operate generically over "whichever task this job
  is," not per-verb code paths.

Adding a new verb (review/11 §3.5 already reserves `embed`, `translate`, `extract`) is: one `Literal`
member, one version constant, one prompt template, and the new Stage that calls it — no change to
`llm.py`, the budget ledger, or the tournament/champion-routing logic.

### §3.2 The actual shipped scheduler interface (added 2026-07-17, R13/`review/33`)

`§5`/`§8` below originally sketched provider allocation and budget as future machinery to build
alongside the tournament. That machinery shipped first, as R13, and any caller — production or
tournament — now goes through it identically:

```python
from citypods.compute.llm_policy import LLMRequestPolicy

job = InferenceJob(
    task="tag",  # or "summarize", "soundbite-select", any Task
    inputs={
        "messages": messages,
        "structured_output": CONTRACT_NAME,
        "llm_policy": LLMRequestPolicy(
            allowed_models=(candidate_model,),  # exact singleton for a tournament contest
            allow_paid=True,                     # tournament calls test paid routes too
            purpose="tournament:tag",             # telemetry only, never branched on
        ),
    },
    recipe_hash=recipe_hash,  # already folds in model_id (§7) -- differs per candidate model,
                              # so each contest's request is a distinct, non-colliding identity
)
result = backend.run_inference(job)
```

`LiteLLMBackend.run_inference` (`citypods/compute/llm.py`) resolves a route via `select_and_reserve`
(`citypods/compute/llm_scheduler.py`) against the CAS-backed ledger (`citypods/compute/llm_budget.py`,
`state/llm_budget.json`) and, on success, returns a `JobResult` whose `.model` field names which model
actually produced it (§7). **On anything less than immediate success — the target route's quota
exhausted, a real 429, or (for Mistral) a genuinely in-flight Worker dispatch — `run_inference` returns
the same `JobHandle` uniformly, never raises.** A tournament/scorer caller must handle this exactly like
a production caller does (R5's `TagsStage` treats any `JobHandle` as "not ready this run, retry next
run" — see `citypods/tags.py::llm_tag_suggestions`); nothing about being a tournament call exempts it
from a route being temporarily unavailable. A deferred tournament request also gets written to the B2
deferred-request registry (`citypods/compute/llm_deferred.py`) and is eligible to complete via the daily
sweep (`scripts/llm_deferred_sweep.py`) the same as any other caller's — **provided the sweep has this
verb's structured-output contract registered in its own process** (see the note in `review/14`'s
"Migrated onto the R13 LLM-scheduler adapters" section: the sweep doesn't import feature modules
automatically; a new tournament module needs its own contract registered there, the same way `tags.py`'s
`ensure_llm_contract()` and `discovery/classify.py`'s import-time registration are).

Because `allowed_models` is an exact singleton per contest, the free-model-protection gate (§5 of
`review/33`) never excludes the candidate under test regardless of `allow_paid`, and — critically — a
tournament call can **never** silently consume production's free-route budget: the allowlist means a
paid-candidate contest is never eligible to fall back onto Gemini's free quota, and a free-candidate
contest (e.g. judging Mistral) draws from that route's *own* ledger entry, not a shared pool with
production tagging traffic. This is `review/33` §11.3's own stated design, restated here concretely for
this doc's tournament to actually cite.

---

## §4. Retrieval-scoped chunking — chapter boundaries as cut points

**Decision: retrieval-scoped from the start**, not full-transcript-in-context. A 6–8hr meeting transcript
plus a ~10K-word agenda runs roughly 70,000–110,000 tokens. Every provider's *context window* handles
that fine (Gemini/DeepSeek: 1M; Mistral: 256K), but Gemini's free-tier **250,000 TPM** cap means a single
~100K-token request burns ~40% of a minute's entire throughput budget — a real pacing concern at
catalog scale, not a context-window problem.

### §4.1 Chunking algorithm

1. For each episode, get chapter boundaries from `episode_served_chapters(ep)`
   (`citypods/chapters.py:9-34` — served-time, already the convention this project uses for "what's
   actually being played," per `review/13`/`review/14`'s prior corrections).
2. Each chapter span `[chapter[i].start, chapter[i+1].start)` is a chunk candidate. Pull the
   corresponding transcript segments from the word-JSON (`transcript_words_url`) by timestamp range.
3. Estimate tokens per span with a conservative word-count heuristic (× ~1.3) — exact tokenization
   differs per provider and isn't worth the complexity; overestimate rather than risk a rejected request.
4. If a span fits the target model's context **and** its TPM budget for one call, send it as one chunk.
5. **Fallback — oversized chunk:** if a single chapter span (e.g. a 90-minute item discussion) doesn't
   fit the target model, escalate **that chunk only** to a larger-context/higher-TPM tier
   (Gemini/DeepSeek's 1M windows), not the whole episode.
6. **Fallback — no chapters:** if an episode has no chapters (some lack H12 chaptering), treat the whole
   transcript as one chunk, same escalation rule. Given two of three providers offer 1M windows, "doesn't
   fit anywhere" should be rare; not deeply engineered here — if it ever occurs, the honest answer is a
   fixed-size fallback split, flagged as a low-priority edge case, not designed further in this pass.

### §4.2 Recombining chunk outputs — different per task shape

- **Tags**: chunk-level tag proposals (with evidence spans, per `review/14`'s existing shape) simply
  **union and dedup** across chunks into the episode's final tag list — no reconciliation needed, this is
  already how the rules engine works.
- **Summaries**: need a coherent narrative, not concatenated chapter summaries. Use the standard
  **map-reduce pattern**: one summary per chapter (chunks already fit easily), then a second synthesis
  call that takes *all* the chapter-level summaries (now short — a few hundred words total) and produces
  one episode-level summary. The second pass is cheap on every provider regardless of the first pass's
  cost, since its input is tiny by then.
- **Soundbite selection**: operates on chunk-local candidates (a soundbite is inherently a bounded clip)
  — no cross-chunk reconciliation needed beyond picking the best candidate(s) episode-wide, itself a
  small, cheap final-pass comparison over a handful of candidates, not a large-context call.

### §4.3 Prompt strategy — one shared template per task-verb, not per-provider

Research confirms there's no true prompt portability across providers — small phrasing/ordering changes
shift outputs meaningfully. The standard mitigation is a **structured prompt** (explicit
Context/Instructions/Output-format sections), which narrows but doesn't eliminate the gap. **Start with
one shared structured template per task-verb** (`TASK_PROMPTS[task]`), not provider-specific variants —
matches the "don't invent a new dependency's worth of complexity you don't need yet" instinct already
established this session, and the tournament (review/34) is exactly the mechanism that would reveal *if*
per-provider prompt tuning is ever actually warranted, so it's better deferred than pre-built.

---

## §5. Provider allocation — reusing H5's ordering engine, not new prioritization code

**The Gemini-primary / DeepSeek-secondary policy is structurally identical to H5's existing
windowed-recency backlog ordering** (`citypods/ops/workqueue.py`, prod policy `recency:{desc,
within_days:30}`, `review/11` §4 H5 row) — apply that comparator registry to LLM tasks rather than
writing new prioritization logic.

**Division of responsibility, concrete as of R13 shipping (2026-07-17, see §3.2):** H5's
comparator/workqueue decides *which episode's tag/summarize/etc. job a Stage attempts next* — unchanged
by R13, still each feature Stage's own backlog policy, per `review/33` §11.4 ("R13 only ever answers 'is
a route eligible right now,' not 'whose turn is it'"). R13's `select_route`/`select_and_reserve`
(`citypods/compute/llm_scheduler.py`) decides, for *that already-chosen* job, which route serves it —
Gemini-primary/DeepSeek-secondary falls out of its ranking (free before paid, cheapest currently
eligible price after any price-window gate,
`§5` of `review/33`), not from separate ordering code here.

- **Gemini (primary):** process LLM tasks newest-meeting-first within the current recency window; when
  the window's exhausted, widen it and continue until Gemini's daily quota (RPD or TPM, whichever binds
  first) is hit for that run. This is a genuine hard stop, now concretely enforced by
  `citypods/compute/llm_budget.py`'s CAS-backed `state/llm_budget.json` ledger (Pacific-midnight RPD
  reset, per-minute RPM/TPM windows) — not a policy choice, and not something this doc needs to build,
  since it already shipped as R13. The next scheduled run resumes where it left off (a deferred
  `JobHandle`, §3.2), the same "pick up next run" pattern already used for ASR/diarization backlogs.
- **DeepSeek (secondary):** its own small daily $ budget — implemented, per-route, in the same
  `state/llm_budget.json` ledger (`RouteLedger.cost_used`/`cost_cycle_key`, reusing
  `citypods.compute.budget.cycle_key` directly, per `review/33` §10.1 — not a second ledger). Used for
  (a) genuine overflow if Gemini's free tier is ever insufficient, and (b) tournament participation (review/34).
- **Mistral:** tournament judge + occasional per-verb champion (review/34), not a default production channel —
  see the capacity math in §5.1, which shows why. Only reachable when a caller's `LiteLLMBackend`
  instance has `dispatch_url` configured (R13's `_available_transports()`, §3.2) — a tournament caller
  needing to judge/candidate Mistral must construct its backend with `dispatch_url` set, exactly like
  the deferred-request sweep does to reach both transports from one instance.

### §5.1 Capacity math (corrected, for planning against)

Mistral's ~1B tokens/month free cap and ~2 RPM (planned as 1/min) give two *different* ceilings:
**~50,000 requests/month by token budget** (1B ÷ ~20K tokens/request) vs. **~43,200 requests/month by
rate limit** (1/min × 60 × 24 × 30). The rate limit is the binding constraint, and the two aren't as far
apart as "1B tokens" sounds in isolation. This is ample headroom for judge/tournament duty (periodic,
sampled, not every production call) but would be tight if Mistral ever became a full production channel
for even one verb across a large catalog — e.g. a hypothetical 2,000-city, 6-meetings/month, 3-verb
scale already implies ~36,000 calls/month for *one* verb alone, close to Mistral's own ceiling if it bore
that load solo. Keep Mistral scoped to judging + occasional champion duty, not a default channel.

### §5.2 The dual-output data-model implication

During any period a task-verb has an active tournament or a non-default champion, the **same
episode+task can have outputs from more than one provider** — a canonical/served one and a
comparison-only shadow one. See §10 for the exact field shape; the short version is a `source_provider`
field on the stored output, and shadow/comparison outputs are stored separately, never racing the
canonical write.

**What R13 already gives this for free, and what it doesn't (2026-07-17):** `JobResult.model`/
`JobHandle.model` (`citypods/compute/base.py`) already record which model *actually* produced a given
result — populated by `run_inference` for every call, policy-bearing or not (§3.2, §7) — so
`source_provider` above is not new bookkeeping to invent, just this field, read and stored by whichever
feature/tournament code persists the output. What R13 does **not** provide, because it's out of its
scope (`review/33` §10.5 — "no job records, no lifecycle states... nothing about *which* jobs are
waiting on it"): a durable place to store more than one contest candidate's output for the *same*
episode+verb at once, or which one is currently canonical/served vs. shadow-only. That storage — the
`source_provider`-tagged shadow block described below in §10 — is still the tournament module's own
responsibility to build, not something R13's ledger or registry happens to already cover.

### §5.3 DeepSeek pricing and batch boundary

**The effective-dated rate card is compiled from YAML.** This pipeline is already schedule-driven, not
real-time, so the scheduler can apply the current UTC window and hold flexible work for the route's
cheapest recurring window without adding a provider-specific branch. Cache-hit pricing is intentionally
omitted: the pipeline cannot predict or control the hit ratio, so only configured input/output rates
participate in admission and accounting.

- **Effective 2026-08-16 16:00 UTC:** Flash is `$0.22` input / `$0.66` output and Pro is
  `$0.66` input / `$1.98` output per million tokens during off-peak. Peak windows are
  `01:00–04:00` and `06:00–10:00 UTC`, with a `2.0` multiplier. These values live in the two
  DeepSeek route pricing periods in `config/provider_limits.yml`.
- **Batch dispatch:** DeepSeek does not provide a batch API. No batch transport is implemented; a
  different URL alone would not make the existing chat-completion transport batch-capable.
- **Flexible-work gate:** DeepSeek-bound dispatch — both tournament/benchmark calls (review/34) and
  any overflow production work (§5) — waits for the next cheapest recurring window when the work is
  not time-sensitive. A caller deadline overrides the wait when the cheaper window would be too late.
  This is implemented in both the Python scheduler and dispatch Worker; no separate queue or
  route-specific transport metadata is required.

---

## §6. Quality tournament & cost-gated champion routing — moved to review/34

**This section moved to its own doc, [`review/34`](34-llm-quality-tournament-champion-routing.md), on
2026-07-17.** In brief: a weekly, per-task-verb pairwise tournament (Promptfoo, round-robin across the
three providers, a judge never grades its own family) determines whether a challenger clears a win-rate
margin over the current production champion; a cost-gated GitHub-issue ticket then lets the maintainer
approve or decline a routing switch via checkbox. It is still unbuilt — no tournament/champion/Promptfoo
code exists anywhere in this repository as of this pass. See review/34 for the full method, the concrete
`LLMRequestPolicy` interface a tournament caller uses against this adapter (its old review/34 §6), and how it
composes with [`review/35`](35-llm-confidence-calibration-human-review.md)'s separate per-candidate
ground-truth calibration module (used by R5's tagging, `review/14`) — the two are complementary, not
overlapping designs.

---

## §7. Versioning — per-task-verb, registry-driven

Matches the existing `ASR_PIPELINE_VERSION`/`TRANSCRIPT_PIPELINE_VERSION` convention exactly
(`citypods/stages.py:1231-1234`) — one version constant per task-verb (`TASK_VERSIONS["tag"]`,
`TASK_VERSIONS["summarize"]`, ...; shipped as `citypods/compute/llm.py::TASK_VERSIONS`, a plain
`dict[Task, str]` — confirmed registry-keyed, no per-verb branching), so a version bump for one verb
re-derives only that verb's outputs, never the others. `recipe_hash` folds in `model_id` + `prompt_hash`
+ the task's version constant + relevant input fingerprints, per `review/11`'s already-established
LLM-verb convention. **The one addition the multi-provider/tournament design requires:** the recipe
must record *which provider* produced a given output, so a champion-routing switch (review/34 §5) is a version
bump like any other model change — re-deriving through the same content-addressed mechanism, not a
special case. **Shipped as of R13 (2026-07-17):** `JobResult.model`/`JobHandle.model`
(`citypods/compute/base.py`) already carry the actually-resolved model on every result, precisely
because `recipe_hash` is computed *before* a policy-driven call resolves which model serves it and so
can't itself encode that choice (`review/33`'s own rationale for adding the field) — a champion-routing
switch reads this field to know which candidate's output it's looking at, rather than needing new
plumbing.

---

## §8. Budget — soft cap, deferred cost-estimation tooling

### §8.1 Soft cap behavior

Per-provider daily/monthly $ budgets, mirroring H14d's provider-cycle dollar ledger (reserve-then-settle,
`review/11` §4 H14d row). "Soft" means: hitting a cap **degrades gracefully rather than breaking
anything** — e.g. a provider's channel simply stops accepting new work until its next cycle, and callers
(TagsStage, future SummarizeStage) already tolerate "the LLM output isn't ready yet, skip and pick it up
next run," per the existing eventual-consistency stage pattern. No hard pipeline failure either way.

**Shipped as of R13 (2026-07-17):** this is `citypods/compute/llm_budget.py`'s `LLMBudget`/
`RouteLedger`, CAS-backed at `state/llm_budget.json` (`review/33` §10) — not a separate ledger to build
alongside the tournament, the *same* object R13 already uses for RPM/RPD/TPM quota. A soft-cap breach
returns a deferred `JobHandle` (§3.2) rather than raising, matching the "skip and pick it up next run"
behavior this section already called for before the concrete mechanism existed.

### §8.2 Cost-estimation tooling — deferred, not built now

**Decision: ship the ledger + per-task-verb telemetry as part of this item, but do not build a
forward-looking cost estimator yet.** Let real numbers accumulate from actual R5/R6 usage; "cost of a new
city" or "cost of a new feature" then becomes a simple multiplication from measured per-call averages
instead of a guess made before any real data exists. This is literally how H14d's own GPU budget tuning
came about — built from live telemetry, not upfront estimation (`review/11` §4 H14d row). The weekly
champion-stats ticket (review/34 §5) is the first real consumer of this telemetry once it exists.

---

## §9. Rate-limited LLM dispatch Worker (ROADMAP R10)

**Numbered R10, not R2-point-something — deliberately, to avoid the renumbering churn a mid-sequence
insert caused earlier this session.** Sequenced *second* in the ROADMAP priority table, right after R1,
so its async enqueue/poll contract exists and is testable by the time this item (R2) is built — R2's
Mistral integration is the first thing that needs it. It is not itself a LiteLLM runtime; LiteLLM stays
in the Python adapter or in an explicitly configured LiteLLM Proxy upstream.

### §9.1 Problem

Mistral's free tier is ~1-2 RPM. Pacing that from a GitHub Actions runner means the runner sits mostly
idle between calls — wasted allocated runner-minutes, and arguably borderline GitHub Actions ToS
territory for a job whose actual work is a small fraction of its wall-clock time.

### §9.2 Why Cloudflare Workers specifically

Verified 2026-07-14: Cloudflare's free-tier CPU-time cap (10ms/invocation) **does not count time spent
awaiting a `fetch()` response** — only active CPU cycles. A Worker that fires one request to a
rate-limited provider and awaits the reply burns a few ms of real CPU even if the wall-clock invocation
takes seconds. This is precisely why the "runner mostly waiting" problem doesn't recur here — it's a
platform property, not a workaround. Cron Triggers are free-tier available (5/account, 3/Worker; a single
per-minute trigger is nowhere near that cap). This project already has one live Cloudflare Worker
(`workers/granicus-media-proxy`) and prior deployment experience to build on. Other free providers were
considered (AWS Lambda / GCP Cloud Functions bill wall-clock duration including I/O wait, which
reintroduces the exact problem being solved; Deno Deploy is a plausible alternative but without this
project's existing deployment precedent) — Cloudflare Workers is the clear default given the CPU-time
semantics and existing operational familiarity.

### §9.3 Design

- New Worker (own directory, e.g. `workers/llm-dispatch-proxy/`, following `granicus-media-proxy`'s
  existing structure/secrets conventions).
- Exposes an **OpenAI-shaped asynchronous queue protocol**. It accepts the same normalized request
  shape LiteLLM uses, but because `POST` returns `202` rather than a completed `200` response, it is
  consumed by R2's dispatch transport, not configured as a normal LiteLLM `base_url` provider.
- The configured upstream is also OpenAI-shaped. It may be a provider endpoint that already accepts
  that shape (Mistral is the first route) or an OpenAI-compatible LiteLLM Proxy that performs native
  provider translation for a provider whose API is not OpenAI-shaped. The Worker stays provider-
  agnostic at the wire-format level: provider/model route selection is configuration, not a new client
  implementation in JavaScript.
- Internally: a Cron Trigger (per-minute or the tightest interval the target provider's rate limit
  allows) drains a small pending-request queue and issues **one** request per allowed interval to the
  real provider, writing the result back to R2 (object storage) — reusing the R2 CAS pattern already
  established for the coordination control-plane (H17).
- **No new synchronous coordination needed on the Actions side.** The pipeline writes a pending request
  to R2, then the next scheduled run checks whether a result exists yet — exactly the same "stage skips
  if the artifact isn't ready, retries next run" pattern already used for ASR/diarization backlogs and
  the `TagsStage` no-transcript-yet case (`review/14`). This is the existing pattern applied again, not a
  new one.

### §9.4 Current implementation contract

**2026-08-06: extended multi-provider, see [`review/41`](41-multi-provider-llm-dispatch.md).** The
single-`MODEL_ID`/single-Mistral shape described in this subsection was the Worker's *first*
implementation; review/41 replaces the fixed `UPSTREAM_*` Wrangler vars with a compiled
`config/provider_limits.yml` registry (per-provider `api_base`/multiple accounts) and the "durable
one-request-per-interval gate" mentioned below with a per-route/per-account R2 ledger, so a route's own
`rpm`/`rpd`/`tpm` *ceiling* is enforced per route rather than one global interval sized for Mistral.
**This is quota enforcement, not a throughput guarantee** — the ledger stops a route from ever being
over-dispatched, but the Cron Trigger still claims and dispatches at most one request per tick
regardless of any route's `rpm`, so real throughput for a route is `min(its own rpm, one/tick)` until a
future pass loops dispatch within a tick (review/41 §4's explicitly accepted limitation). A 10-RPM
route does not receive 10 Worker calls/minute today. Routes that declare only `concurrency` (no
`rpm`/`rpd`/`tpm` — today the DeepSeek paid routes) are fail-closed in `routeAvailable`: the Worker's
R2 ledger does not model real-time concurrency, so these routes are rejected outright rather than
treated as unlimited; they are available only to the Python scheduler's direct path, which can enforce
its own concurrency tracking. The async queue boundary itself (`202`/poll,
`stream: true` rejected, R2 conditional writes) is unchanged.

The first Worker implementation keeps the queue boundary explicit: `POST /v1/chat/completions` returns
`202` with a `Location` for `GET /v1/requests/{id}`, which returns the upstream OpenAI-shaped response
once the Cron dispatcher has completed it. This is the asynchronous form required by the durable
R2-handoff design; `stream: true` is rejected. `MODEL_ID` is a provider-qualified public route
(currently `mistral/mistral-large-3`); the Worker rejects a request naming another provider/model.
`UPSTREAM_REQUEST_MODEL` optionally separates the upstream wire value from that public route, which is
needed when the upstream is a LiteLLM Proxy rather than the provider's own endpoint. The upstream path,
bearer secrets, request/response byte caps, retry limits, processing timeout, and dispatch interval are
Wrangler configuration, with the upstream base URL required to be HTTPS. R2
`etagMatches`/`etagDoesNotMatch` conditional writes protect both request claims and the durable
one-request-per-interval gate. The dedicated Worker bucket is ephemeral/derivable and intentionally
separate from the Python catalog state and `RoutingStorage` coordination prefixes. The R2 Python adapter
must persist the returned Worker `ref`/poll URL as the `JobHandle`, and reconcile the completed
OpenAI-shaped response into the normal `JobResult`; it must not ask LiteLLM to synchronously consume the
Worker's `202` response.

---

## §10. Data model deltas

**Status as of 2026-07-17: items 3/4/6 below are shipped (R2/R10/R13); items 1/2/5 are each a real
feature's own data model, still to build — 1 by R5/R6 individually, 2 and 5 by the tournament module.**

1. **LLM-verb outputs on `Episode`/the record** — `tags` already reserved and shipped (`review/14`,
   `Episode.tags`/`chapter_tags`/`llm_tag_candidates`); analogous fields for `summary` (already exists,
   `citypods/models.py`, currently populated by non-LLM paths) and future verbs follow the same shape:
   `{value, source_provider, source: "rule"|"llm"|"human", recipe_hash, confidence?}`. `source_provider`
   is `JobResult.model`/`JobHandle.model` (§7), not a new field to invent.
2. **Shadow/comparison outputs** (§5.2) — stored separately from the canonical/served value, keyed by
   `(task, provider, recipe_hash)`, never overwriting the canonical field. Exact storage location (record
   dict vs. a dedicated CAS block) is an open implementation question — lean toward a dedicated block
   (mirrors how `speakers`/diarization output gets its own `ARTIFACT_BLOCKS` entry, `citypods/records.py`
   §"Extensibility" comment) so tournament data doesn't bloat the primary record read/write path. **Not
   provided by R13** — its ledger/registry are explicitly scoped to never track *which* jobs are pending,
   only route capacity (§5.2, `review/33` §10.5) — this remains the tournament module's own storage to
   design and build.
3. **`TASK_VERSIONS`, `TASK_PROMPTS`** — shipped as `citypods/compute/llm.py::TASK_VERSIONS`/
   `TASK_PROMPTS`, plain `dict[Task, ...]` registries, keyed by `Task`, per §3.1/§7.
4. **Budget ledger** — shipped as `citypods/compute/llm_budget.py`'s `LLMBudget`, CAS-backed at
   `state/llm_budget.json`, mirroring H14d's `compute_budget.json` shape, per-route (not just
   per-provider — Gemini/DeepSeek-flash/DeepSeek-pro/Mistral each get their own ledger row), per-cycle,
   soft-cap semantics (§8.1).
5. **Champion routing config** — which provider is currently dispatched for each task-verb; updated only
   by the weekly-ticket checkbox flow (review/34 §5), never silently. **Concretely, as of R13:** this is each
   feature's own `LLMRequestPolicy(allowed_models=(...))` construction site — for R5, `config/
   site_config.yml`'s `tagging.llm_model`, read into the singleton allowlist `citypods/tags.py::
   llm_tag_suggestions` pins to (§3.2, `review/14`). A champion-routing switch updates that config value;
   no separate "which provider is current" state needs inventing beyond what each feature already reads.
6. **Async dispatch state** — a Worker-backed job stores the provider-qualified `model_id`, request
   fingerprint, Pydantic-generated `response_format`, Worker `ref`/poll location, and status
   (`queued`/`processing`/`completed`/`failed`) as ephemeral coordination state. The `JobHandle` also
   retains the stable response-contract name, so a restart can validate a completed response locally.
   It is not the canonical LLM artifact and is not a second output schema: reconciliation maps the
   Worker's completed OpenAI-shaped response into the same `JobResult` used by direct LiteLLM calls,
   after which the task-specific content-addressed output is written by the normal R2 pipeline path.
   Queue-owned corrective re-asks are deferred until the Worker persists an explicit validation-attempt
   transition; a scheduler may still batch, delay, or route independent cacheable chunk jobs freely.
   **Shipped, generalized beyond just Worker-backed jobs:** R13's deferred-request registry
   (`citypods/compute/llm_deferred.py`, `state/llm_deferred/*.json`) covers this same "pending job"
   shape uniformly for a genuine Worker dispatch *and* a deferred-direct request (nothing eligible yet,
   or a reactive 429), per §3.2 — not a Worker-specific mechanism as originally scoped here.

---

## §11. Module / file plan (exact, where confirmed against existing conventions)

**Status as of 2026-07-17** — shipped: `citypods/compute/llm.py`, `llm_policy.py`, `llm_budget.py`,
`llm_scheduler.py`, `llm_deferred.py`, `workers/llm-dispatch-proxy/` (R2/R10/R13). **Confirmed not yet
built:** chapter-boundary chunking (no `citypods/compute/chunking.py` or equivalent exists —
`citypods/tags.py`'s actual implementation truncates agenda/transcript text to a fixed character budget
instead, §4's escalation design is unimplemented); `citypods/ops/workqueue.py` has no LLM-task recency
extension (R5's `TagsStage` orders episodes via the same `_materialize_set` every other stage uses, not
a dedicated comparator); and the tournament/champion-routing module + weekly-ticket workflow (review/34) itself.

- `citypods/compute/llm.py` — **shipped.** LiteLLM-backed `Backend` adapter plus the Worker enqueue/poll
  transport, `JobHandle` → `JobResult` reconciliation (§3/§9), and (added by R13, not anticipated when
  this list was first written) `_available_transports()`, the registry/ledger wiring, and reactive
  429 handling (§3.2).
- `citypods/compute/llm_policy.py` / `llm_budget.py` / `llm_scheduler.py` / `llm_deferred.py` —
  **shipped (R13, not in the original plan here since it predates R13's design).** Route capability
  types + `LLMRequestPolicy`/`ROUTES`; the CAS quota+cost ledger; the pure selection function +
  CAS reservation wrapper; the B2-backed deferred-request registry + sweep. See `review/33` for the
  full module breakdown — a new tournament module sits *beside* these, importing `llm_policy`/
  `llm.py` the same way any feature Stage does (§3.2), not modifying any of them.
- `citypods/compute/chunking.py` (or similar) — **not built.** Chapter-boundary chunking + fallback
  escalation (§4.1), map-reduce recombination helpers (§4.2). R5 ships without this today (fixed
  character-count truncation instead); out of this revision pass's scope (not part of the tournament),
  flagged here only for accuracy.
- New per-task-verb prompt templates, one file/entry each (§4.3) — **not built** as a separate
  file/registry; `citypods/compute/llm.py::TASK_PROMPTS` exists as a plain dict today.
- `citypods/ops/workqueue.py` — **not extended.** Still each feature Stage's own backlog policy, per §5.
- New tournament/champion-routing module + the weekly-ticket workflow (`.github/workflows/`, matching
  `availability-digest.yml`'s cadence/structure precedent) + its companion checkbox-parsing Action —
  **not built**; see [`review/34`](34-llm-quality-tournament-champion-routing.md) (this doc's former §6,
  now its own doc) for the full design. Depends on `citypods/compute/llm_policy.py`'s
  `LLMRequestPolicy`/`ROUTES` and `llm.py`'s `LiteLLMBackend` (shipped) per §3.2 above and review/34 §6,
  and needs its own structured-output contract registered wherever it calls `reconcile()` (review/34 §6's
  callout).
- `workers/llm-dispatch-proxy/` — **shipped**, new Cloudflare Worker (§9.3).

---

## §12. Tests

**Status as of 2026-07-17:** the first two bullets and the versioning/budget bullets below are already
covered — `tests/test_compute_llm.py`, `tests/test_compute_llm_scheduler.py`,
`tests/test_compute_llm_budget.py`, `tests/test_compute_llm_deferred.py` (R13) exercise this adapter's
actual behavior, including cases this list didn't originally anticipate (reactive 429 blocking, the
settle-vs-release distinction, deferred-registry round-trips). Chunking, tournament, champion routing,
and the flash/pro tier bullets below remain unwritten — no chunking module and no tournament module
exist yet (§11) for these tests to cover.

- `citypods/compute/llm.py`: direct `InferenceJob` → LiteLLM call translation is deterministic and
  mockable (no real network in CI, matching the existing LLM-mocking convention already used for
  `review/14`'s tests); the same job maps to a provider-qualified Worker enqueue request when the
  selected route is rate-limited.
- Worker transport: a queued request returns a `JobHandle`, polling a completed fixture maps the
  OpenAI-shaped response to the same `JobResult`, a pending result remains deferred, and a failed
  result is retryable without exposing prompts or provider error bodies. Tests must prove a provider
  mismatch is rejected and that a LiteLLM Proxy route can use a distinct `UPSTREAM_REQUEST_MODEL`.
- Chunking: a fixture episode with chapters splits at chapter boundaries; a fixture without chapters
  falls back to whole-transcript; an oversized single-chapter fixture escalates to the larger-context
  tier, not the whole episode.
- Recombination: tag union/dedup across chunks; summary map-reduce produces one coherent output from
  multiple chapter-level inputs.
- Versioning: bumping one task's version constant re-derives only that task's outputs, not others (same
  test shape as `review/13`'s per-field `meeting_page_hash` tests).
- Tournament: round-robin assignment never uses a candidate as its own judge, for all 3 pairs; position
  bias is canceled by running both orderings; tie-handling matches the configured policy.
- Champion routing: a challenger below the required win-rate margin produces no checkbox proposal; one
  above it does; checking a box applies the config change and clears the checkbox on the next ticket.
- Flash/pro tier: a v4-pro fixture win over v4-flash below the margin produces no proposal, one above it
  does, using the same code path as a cross-provider champion switch (no separate tier-switch logic).
  A flash-vs-pro judged comparison is asserted to never use a DeepSeek-family model as judge.
- Budget: a soft-cap breach degrades (skips further dispatch for that provider/cycle) without failing the
  pipeline.

---

## §13. Risks

- **Every pricing/rate-limit figure in §2 is a snapshot from 2026-07-14 research and will drift** —
  re-verify against each provider's live docs immediately before implementation, not from this doc alone.
- **LiteLLM is a new dependency in the untrusted-output-sensitive LLM call path** — audit exactly what it
  logs/transforms before treating it as equivalent to a hand-rolled client for the SECURITY.md
  untrusted-output rule.
- **The checkbox-approval Action is new machinery** (no direct precedent in this codebase, only the
  well-known external Renovate pattern to model it on) — get the parsing/clearing logic right, since a
  bug here could silently apply or fail to apply a champion-routing decision.
- **Mistral's rate limit is tight enough that even judge/tournament duty needs real pacing** — confirm
  the R10 Worker is genuinely ready before R2's Mistral integration is tested, not assumed ready.
- **DeepSeek's V4 off-peak discount window (§5.3) was unconfirmed at research time** — the 16:30–00:30
  UTC 50–75% discount is documented for V3/R1, not yet officially confirmed for V4. Verify before the
  scheduling preference is treated as a guaranteed saving rather than a "might still apply" bonus.

---

## §14. Sequencing & dependencies

R1 → **R10** (dispatch Worker, built second specifically so it's ready in time) → **R2** (this item) →
R5 (tags, LLM-assist path) / R6 (auto-summaries, soundbite selection), both first feature consumers.
Depends on H13 (shipped) for the `Backend` Protocol and reserved verbs, H5 (shipped) for the
windowed-recency ordering engine being reused, H14d (shipped) for the budget-ledger pattern being
mirrored, H15 (shipped) for the periodic-calibration cadence being mirrored, H17 (shipped) for the R2
(object storage) CAS pattern the Worker's result-handoff reuses.

---

## §15. Migration / backfill

No backfill on first ship — this is new infrastructure with no prior LLM outputs to migrate. Back-catalog
recalculation only ever happens as an explicit, human-approved action via the weekly champion-stats
ticket (review/34 §5) once a real champion switch is decided — never automatic, never silent.

---

## §16. Acceptance

An `InferenceJob(task="tag", ...)` (or any reserved verb) dispatches through the LiteLLM adapter to the
policy-selected provider. A direct route returns a `JobResult`; a rate-limited route returns a
`JobHandle`, and a later reconcile maps its completed Worker response to that same `JobResult` without
runner-side idle-waiting. Large transcripts are chunked at chapter boundaries, escalating individual
oversized chunks rather than the whole episode. Adding a new task-verb requires no change to
dispatch/budget/tournament code — only a new Literal member, version constant, prompt template, and
calling Stage. The weekly champion-stats ticket accurately reports quality, live per-month cost, and
one-time back-catalog cost per option, proposes a switch only when a challenger clears the required
win-rate margin, and a checked box both applies the change and clears itself. A soft budget-cap breach
degrades gracefully with no pipeline failure.

## Proposed GitHub issues (not filed — batch review pending)

**Status as of 2026-07-17** — issues 1, 2, 5, and 8 shipped (as R10/R13, not filed as separate issues
against this list, but the work landed); issues 3, 4 remain open and are what's actually left of this
doc's own proposed work. Issues 6/7 (tournament, champion-stats ticket) moved to
[`review/34`](34-llm-quality-tournament-champion-routing.md)'s own proposed-issues list along with the
rest of that design; kept here only as a pointer, not duplicated.

1. ~~R10: `workers/llm-dispatch-proxy/` Cloudflare Worker~~ — **shipped.** OpenAI-shaped asynchronous
   enqueue/poll transport, Cron-paced dispatch, and R2 result handoff.
2. ~~`citypods/compute/llm.py` — LiteLLM-backed `Backend` adapter + `TASK_VERSIONS`/`TASK_PROMPTS`
   registries~~ — **shipped**, plus R13's `LLMRequestPolicy`/scheduler/budget layer this list didn't
   originally anticipate (`citypods/compute/llm_policy.py`/`llm_scheduler.py`/`llm_budget.py`/
   `llm_deferred.py`, §3.2).
3. **Open.** Chapter-boundary chunking + map-reduce recombination (tags vs. summaries) — §4, no module
   exists (§11); R5 ships today with fixed-character truncation instead.
4. **Open.** Provider allocation policy — extend `citypods/ops/workqueue.py`'s comparator registry to
   LLM tasks — not done; R5's `TagsStage` still orders episodes via the default materialization order,
   not a dedicated LLM-task comparator (§5, §11).
5. ~~Budget ledger (H14d-style, per-provider, soft-cap)~~ — **shipped** as `citypods/compute/
   llm_budget.py` (§8.1, §10 item 4), per-*route* not just per-provider. No forward cost-estimator yet
   (§8.2) — that part is still correctly deferred, not a gap.
6. **Moved to `review/34`.** Tournament: Promptfoo-based round-robin pairwise comparison, weekly cadence,
   bias mitigation (order-swap, tie policy, human-calibration check) — see review/34 §6 for how it
   interfaces with the now-shipped adapter, and review/34's own proposed-issues list for tracking.
7. **Moved to `review/34`.** Weekly champion-stats GitHub issue + checkbox-approval Action (quality, cost,
   back-catalog-cost, apply-and-clear) — depends on 6.
8. ~~DeepSeek off-peak/batch scheduling preference~~ — **the off-peak half shipped** as R13's gate 5
   (`_active_multiplier`/`_next_discount_window_end`, `citypods/compute/llm_scheduler.py`, using exactly
   the `PeakWindow(tz="UTC", start=time(16,30), end=time(0,30), multiplier=0.5)` this section specified).
   **The batch-submission half remains open** and unconfirmed (§5.3 — whether DeepSeek exposes a real
   submit/poll batch endpoint or "async already gets the off-peak rate" is the same open question).
