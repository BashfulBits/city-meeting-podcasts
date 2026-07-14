# review/27 — LLM Backend & Provider Routing (Phase R)

**Maturity: L3 · authored 2026-07-14 · breakout of [`review/11`](11-technical-design-roadmap.md) Phase R
· ROADMAP R2 (LLM backend) + R10 (rate-limited dispatch Worker, numbered out of table-position on
purpose — see ROADMAP's insert note) · issues not yet cut, batch review pending**

> This is the first real adapter for the H13-reserved `tag`/`summarize`/`soundbite-select` compute verbs.
> R5 (topic tags, LLM-assist path) and R6 (auto-summaries, soundbite selection) are its first feature
> consumers; neither builds this under its own time pressure. Design decided across a planning
> conversation with the maintainer on 2026-07-14; every provider/pricing/rate-limit figure below was
> verified via web research on that date and should be re-checked before implementation, since these
> figures move.

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
ongoing allowance — its actual advantage is being extremely cheap once paid (deepseek-v4-flash:
$0.14/M input cache-miss, $0.28/M output), not free. Gemini's free tier is the one that's genuinely
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
exact champion-routing mechanism from §6, not a new one.** This mirrors the provider-level policy
precisely: v4-flash is the cheap baseline (analogous to Gemini's free tier at the provider level),
v4-pro must clear the same required win-rate margin (§6.3) to be proposed as the DeepSeek-side tier for
a given verb — e.g. "may do fine for simpler summarization tasks" on flash, while a verb that needs more
reasoning capability (potentially `tag`, which has to weigh nuanced taxonomy judgment) might justify the
upgrade. **No new versioning machinery needed:** `recipe_hash` already folds in `model_id` (§7), so
switching a verb between `deepseek/deepseek-v4-flash` and `deepseek/deepseek-v4-pro` is already a normal
version bump through the existing mechanism — this is the existing design generalizing cleanly, not a
gap. **Judging note:** a flash-vs-pro comparison for a given verb still needs a non-DeepSeek judge
(Gemini or Mistral) per the same self-family-bias rule as the provider-level tournament, since both
candidates share the DeepSeek family.

**Why three providers, not two:** the round-robin tournament (§6) requires exactly this — with 3 models
and the rule "a judge can never grade its own family," a round robin covers all 3 pairs with zero
self-judging, by construction. Two providers can't do this (whichever one isn't being compared has to
judge, which is fine for exactly one pair but leaves the other pair with no independent judge).

---

## §3. Architecture — LiteLLM adapter, our own async dispatch transport beside it

**Decision: adopt LiteLLM as a dependency**, not a hand-rolled per-provider client. Unlike the
Pagefind-vs-MiniSearch call in `review/13`, this is a pure-Python pip dependency (no new build toolchain)
that already speaks Gemini, DeepSeek, Mistral, and effectively every other provider likely to matter
later — "adopt to other providers" (the requirement driving this whole design) becomes near-zero marginal
code per new provider, which a hand-rolled adapter can't match. Needs to clear `review/22`'s
dependency-pinning policy like any new dependency.

- `citypods/compute/llm.py` — new. A `Backend`-conforming adapter (`citypods/compute/base.py`'s
  `Backend` Protocol) that builds one normalized chat request from `job.task`/`job.inputs` and has two
  execution paths:
  - **direct**: call `litellm.completion(model=..., messages=..., response_format=...)` and map the
    normalized response to a `JobResult`;
  - **rate-limited**: submit that same normalized request to the R10 Worker and return a `JobHandle`.
    A later reconcile/poll operation fetches the Worker result and maps it to the same `JobResult` shape.
    The H13 union return type is intentional: a direct LiteLLM call completes in-band, while the Worker
    route preserves the no-runner-idle queue boundary.
- LiteLLM owns provider-native wire-format translation, response normalization, and its configured
  retry/fallback behavior. **It does not own provider selection or this project's durable pacing** —
  those depend on our budget/tournament state and the explicit rate-limit policy in §5/§9.
- The R10 endpoint is **OpenAI-shaped asynchronous transport, not a synchronous LiteLLM provider
  endpoint**: `POST /v1/chat/completions` returns `202` + a poll location. Therefore `llm.py` must use
  its enqueue/poll protocol rather than passing the Worker URL as `api_base` to a plain
  `litellm.completion()` call. This keeps LiteLLM as the provider adapter without pretending an async
  queue is a synchronous completion API.
- A Worker deployment may point at (a) a provider's own OpenAI-compatible endpoint, such as Mistral's,
  or (b) an OpenAI-compatible LiteLLM Proxy when the selected provider needs native wire-format
  translation. The Cloudflare Worker never grows provider-specific clients; the latter proxy is a
  separately provisioned LiteLLM runtime and is not hidden inside the JavaScript Worker.
- Model strings follow LiteLLM's `provider/model` convention: `gemini/gemini-3-flash-preview`,
  `deepseek/deepseek-v4-flash`, `deepseek/deepseek-v4-pro`, `mistral/mistral-large-3`.

### §3.1 Extensible task-verb design (per maintainer requirement — no re-architecture to add a verb)

Keep `Task` as a `Literal`, don't loosen it to `str`. It's cheap to extend (one line) and it's the entire
point of H13's "typed for the full verb set up front" pre-1.0 lock — loosening it would undo that
deliberate design decision, not simplify it. The actual extensibility requirement is that everything
**downstream** of the Literal must be registry/dict-keyed by task name, never a chain of
`if task == "tag": ... elif task == "summarize": ...` branches:

- `TASK_VERSIONS: dict[Task, str]` (§7) — one entry per verb.
- `TASK_PROMPTS: dict[Task, PromptTemplate]` (§4.3) — one entry per verb.
- The budget ledger (§8) and tournament (§6) already operate generically over "whichever task this job
  is," not per-verb code paths.

Adding a new verb (review/11 §3.5 already reserves `embed`, `translate`, `extract`) is: one `Literal`
member, one version constant, one prompt template, and the new Stage that calls it — no change to
`llm.py`, the budget ledger, or the tournament/champion-routing logic.

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
established this session, and the tournament (§6) is exactly the mechanism that would reveal *if*
per-provider prompt tuning is ever actually warranted, so it's better deferred than pre-built.

---

## §5. Provider allocation — reusing H5's ordering engine, not new prioritization code

**The Gemini-primary / DeepSeek-secondary policy is structurally identical to H5's existing
windowed-recency backlog ordering** (`citypods/ops/workqueue.py`, prod policy `recency:{desc,
within_days:30}`, `review/11` §4 H5 row) — apply that comparator registry to LLM tasks rather than
writing new prioritization logic.

- **Gemini (primary):** process LLM tasks newest-meeting-first within the current recency window; when
  the window's exhausted, widen it and continue until Gemini's daily quota (RPD or TPM, whichever binds
  first) is hit for that run. This is a genuine hard stop — Gemini enforces its own limits, it's not a
  policy choice. The next scheduled run resumes where it left off, the same "pick up next run" pattern
  already used for ASR/diarization backlogs.
- **DeepSeek (secondary):** its own small daily $ budget (H14d-style provider-cycle dollar ledger —
  reserve-then-settle, per-task/provider cost coefficients, `review/11` §4 H14d row is the direct
  precedent). Used for (a) genuine overflow if Gemini's free tier is ever insufficient, and (b) tournament
  participation (§6).
- **Mistral:** tournament judge + occasional per-verb champion (§6), not a default production channel —
  see the capacity math in §5.1, which shows why.

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

### §5.3 DeepSeek off-peak + batch dispatch — a real, missed-in-first-draft cost lever

**Gap in the first draft of this doc, caught on review: DeepSeek's off-peak and batch discounts were
researched but never actually designed in.** This pipeline is already schedule-driven, not real-time —
exactly the shape these discounts are built for — so this is close to free savings, not a tradeoff.

- **Off-peak window:** DeepSeek has historically discounted 50–75% (V3/R1: 50% off standard models, 75%
  off R1-class reasoning models) during **16:30–00:30 UTC**. Confirmed 2026-07-14: **V4's off-peak
  pricing was not yet officially confirmed** at research time — treat the window as *likely* to still
  apply, verify against DeepSeek's live pricing docs before relying on it, and design the scheduling hook
  so it degrades gracefully (dispatch still works correctly outside the window, just without the
  discount) rather than assuming the discount is guaranteed.
- **Batch dispatch:** DeepSeek supports asynchronous batch submission for non-realtime work, and the
  batch + off-peak discounts **stack** (up to ~75% combined per the research). **Distinguish this from
  LiteLLM's own `batch_completion`**, which is client-side parallel dispatch of synchronous calls, not
  the same thing as a provider's server-side batch-submission-then-poll API with its own discount.
  Confirm at implementation time whether DeepSeek exposes a distinct batch endpoint (OpenAI-Batch-API
  style: submit a JSONL of requests, poll, collect results within a completion window) or whether
  "batching" in DeepSeek's own materials just means "async access already gets you the off-peak rate" —
  the research wasn't fully conclusive on which, and the two have different implementation shapes.
- **Design hook, not a hard requirement:** DeepSeek-bound dispatch — both tournament/benchmark calls
  (§6) and any overflow production work (§5) — should **prefer** scheduling into the 16:30–00:30 UTC
  window when the work isn't time-sensitive (which almost all of it isn't — tournament runs are weekly,
  overflow dispatch already tolerates "picked up next run"). This is a scheduling *preference* in the
  dispatch coordinator, not a blocking constraint — DeepSeek dispatch outside the window should still
  work, just at standard (still cheap) pricing. Given GitHub Actions cron schedules are already flexible
  (`audio.yml`/`asr.yml` already run multiple times/day on independent schedules), aligning a
  DeepSeek-dispatch-preferring run with the discount window is a scheduling decision, not new
  infrastructure.

---

## §6. Quality tournament & cost-gated champion routing

### §6.1 Method — pairwise comparison, not absolute scoring

Research is unambiguous: "which is better, A or B" is far more reliable — for both human and LLM judges
— than "rate this 1–10." Maps directly onto the two dimensions specified: **factual accuracy** (judge
sees the source transcript/agenda excerpt + both candidates, asked which is more faithful, or whether
both are equally faithful/unfaithful) and **style/quality** (judge sees both candidates, asked which
reads better — length, tone, structure).

### §6.2 The round-robin structure

With exactly 3 providers and the rule "a judge never grades its own family," round-robin covers all 3
pairs with a genuinely independent judge every time, by construction — no configuration needed to avoid
self-preference bias:

| Contest | Candidates | Judge |
|---|---|---|
| 1 | DeepSeek vs. Gemini | Mistral |
| 2 | DeepSeek vs. Mistral | Gemini |
| 3 | Gemini vs. Mistral | DeepSeek |

Run per task-verb, on a **weekly cadence** (matching the stated 1–2/week human-time budget and directly
mirroring H15's own periodic-calibration cadence, `review/11` §4 H15 row — same shape, different subject
matter). **Tool: Promptfoo** (MIT, SQLite-backed, no new infra service, already supports Gemini/DeepSeek/
Mistral natively) rather than a hand-rolled comparison harness — fits this project's "no extra
infrastructure" pattern better than building one from scratch.

**Bias mitigation, from research, applied concretely:** run both orderings (A-then-B and B-then-A) per
judged pair to cancel position bias; decide tie-handling up front (recommend: ties count as half a win
each, Elo-style, rather than being dropped — dropping ties silently shrinks the sample and can mask a
genuinely-close result); validate the automated (Promptfoo/LLM-judge) result against the maintainer's own
occasional human read (the stated 1–2/week) — if agreement is low, the automated judge isn't trustworthy
yet for that verb, and champion decisions should lean on the human read until it is.

### §6.3 Cost-gated champion routing — the weekly ticket

**A challenger must clear a required win-rate margin over the current champion to be proposed as a
switch** — not just win more than half the judged comparisons. (Recommend a concrete default, e.g. >60%
win rate over a rolling sample, but this should be a tunable config value, not hardcoded.) If no
challenger clears the threshold for a verb that week, the ticket is FYI-only — no decision is requested,
avoiding weekly decision fatigue for a "nothing changed" week. **"Challenger" includes the within-DeepSeek
flash→pro upgrade (§2.1)**, using the identical mechanism — the ticket doesn't need a separate code path
for "switch provider" vs. "switch tier within a provider," since both are just "propose a different
`model_id`" against the same recipe_hash-driven versioning.

**Weekly "champion stats" GitHub issue, one per task-verb** (or one consolidated issue with a section per
verb — implementation detail to settle when this is built), following the existing conventions this
project already uses for recurring automated issues (`review/11` §4 H4 row: one consolidated issue per
check, a hidden JSON state block in the body for tracking — not an external ledger — matching output),
containing:

1. **Current quality results** — this week's (and recent rolling) win/loss/tie record per pair, for this
   verb.
2. **Cost implication per month** for each option — current champion vs. each challenger, using the real
   per-call cost the budget ledger already tracks (§8) — not an estimate, since ledger telemetry exists
   by the time this runs.
3. **One-time back-catalog recalculation cost** if switching — this is the exact scenario the deferred
   cost-estimation approach (§8.2) was designed for: by the time a real challenger exists, real
   per-call-cost telemetry exists too, so "cost to re-derive verb X across N already-processed episodes"
   is a simple multiplication from measured averages, not a guess.
4. **A checkbox decision block** (one checkbox per real option — keep current champion / switch to
   challenger X, with and without back-catalog upgrade; for DeepSeek specifically, "challenger X" may be
   "DeepSeek v4-pro" while the current champion is "DeepSeek v4-flash," same mechanism) — modeled on the
   well-established
   "Renovate Dependency Dashboard" pattern (checkboxes in a bot-maintained issue that a scheduled Action
   parses and acts on), adapted to this project's own hidden-JSON-state-in-body convention rather than a
   new state-tracking mechanism.
5. **A separate Action, triggered on issue edit**, parses which box (if any) got checked, applies the
   routing config change (updates which provider is dispatched for that verb) and/or enqueues the
   back-catalog recalculation job if requested, then **clears the checkboxes** so the next week's ticket
   starts from a clean decision state.

---

## §7. Versioning — per-task-verb, registry-driven

Matches the existing `ASR_PIPELINE_VERSION`/`TRANSCRIPT_PIPELINE_VERSION` convention exactly
(`citypods/stages.py:1231-1234`) — one version constant per task-verb (`TASK_VERSIONS["tag"]`,
`TASK_VERSIONS["summarize"]`, ...), so a version bump for one verb re-derives only that verb's outputs,
never the others. `recipe_hash` folds in `model_id` + `prompt_hash` + the task's version constant +
relevant input fingerprints, per `review/11`'s already-established LLM-verb convention. **The one
addition the multi-provider/tournament design requires:** the recipe must record *which provider*
produced a given output, so a champion-routing switch (§6.3) is a version bump like any other model
change — re-deriving through the same content-addressed mechanism, not a special case.

---

## §8. Budget — soft cap, deferred cost-estimation tooling

### §8.1 Soft cap behavior

Per-provider daily/monthly $ budgets, mirroring H14d's provider-cycle dollar ledger (reserve-then-settle,
`review/11` §4 H14d row). "Soft" means: hitting a cap **degrades gracefully rather than breaking
anything** — e.g. a provider's channel simply stops accepting new work until its next cycle, and callers
(TagsStage, future SummarizeStage) already tolerate "the LLM output isn't ready yet, skip and pick it up
next run," per the existing eventual-consistency stage pattern. No hard pipeline failure either way.

### §8.2 Cost-estimation tooling — deferred, not built now

**Decision: ship the ledger + per-task-verb telemetry as part of this item, but do not build a
forward-looking cost estimator yet.** Let real numbers accumulate from actual R5/R6 usage; "cost of a new
city" or "cost of a new feature" then becomes a simple multiplication from measured per-call averages
instead of a guess made before any real data exists. This is literally how H14d's own GPU budget tuning
came about — built from live telemetry, not upfront estimation (`review/11` §4 H14d row). The weekly
champion-stats ticket (§6.3) is the first real consumer of this telemetry once it exists.

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

1. **LLM-verb outputs on `Episode`/the record** — `tags` already reserved (`review/14`); analogous
   fields for `summary` (already exists, `citypods/models.py`, currently populated by non-LLM paths) and
   future verbs follow the same shape: `{value, source_provider, source: "rule"|"llm"|"human", recipe_hash,
   confidence?}`.
2. **Shadow/comparison outputs** (§5.2) — stored separately from the canonical/served value, keyed by
   `(task, provider, recipe_hash)`, never overwriting the canonical field. Exact storage location (record
   dict vs. a dedicated CAS block) is an open implementation question — lean toward a dedicated block
   (mirrors how `speakers`/diarization output gets its own `ARTIFACT_BLOCKS` entry, `citypods/records.py`
   §"Extensibility" comment) so tournament data doesn't bloat the primary record read/write path.
3. **`TASK_VERSIONS`, `TASK_PROMPTS`** — new registries in `citypods/tags.py`-adjacent LLM-task module(s),
   keyed by `Task`, per §3.1/§7.
4. **Budget ledger** — new state object mirroring H14d's `compute_budget.json` shape, per-provider,
   per-cycle, soft-cap semantics (§8.1).
5. **Champion routing config** — which provider is currently dispatched for each task-verb; updated only
   by the weekly-ticket checkbox flow (§6.3), never silently.
6. **Async dispatch state** — a Worker-backed job stores the provider-qualified `model_id`, request
   fingerprint, Worker `ref`/poll location, and status (`queued`/`processing`/`completed`/`failed`) as
   ephemeral coordination state. It is not the canonical LLM artifact and is not a second output schema:
   reconciliation maps the Worker's completed OpenAI-shaped response into the same `JobResult` used by
   direct LiteLLM calls, after which the task-specific content-addressed output is written by the normal
   R2 pipeline path.

---

## §11. Module / file plan (exact, where confirmed against existing conventions)

- `citypods/compute/llm.py` — new. LiteLLM-backed `Backend` adapter plus the small Worker enqueue/poll
  transport and `JobHandle` → `JobResult` reconciliation (§3/§9); no provider-specific HTTP clients.
- `citypods/compute/chunking.py` (or similar) — new. Chapter-boundary chunking + fallback escalation
  (§4.1), map-reduce recombination helpers (§4.2).
- New per-task-verb prompt templates, one file/entry each (§4.3).
- `citypods/ops/workqueue.py` — extended, not replaced, to cover LLM-task recency ordering (§5), reusing
  the existing comparator registry.
- New budget ledger module, mirroring H14d's `compute_budget.json` pattern (§8.1).
- New tournament/champion-routing module + the weekly-ticket workflow (`.github/workflows/`, matching
  `availability-digest.yml`'s cadence/structure precedent) + its companion checkbox-parsing Action (§6.3).
- `workers/llm-dispatch-proxy/` — new Cloudflare Worker (§9.3).

---

## §12. Tests

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
ticket (§6.3) once a real champion switch is decided — never automatic, never silent.

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

1. R10: `workers/llm-dispatch-proxy/` Cloudflare Worker — OpenAI-shaped asynchronous enqueue/poll
   transport, Cron-paced dispatch, and R2 result handoff; it is not a synchronous LiteLLM endpoint.
2. `citypods/compute/llm.py` — LiteLLM-backed `Backend` adapter + `TASK_VERSIONS`/`TASK_PROMPTS`
   registries.
3. Chapter-boundary chunking + map-reduce recombination (tags vs. summaries).
4. Provider allocation policy — extend `citypods/ops/workqueue.py`'s comparator registry to LLM tasks;
   Gemini-primary windowed-recency, DeepSeek/Mistral secondary.
5. Budget ledger (H14d-style, per-provider, soft-cap) — ships with telemetry; no forward cost-estimator
   yet (§8.2).
6. Tournament: Promptfoo-based round-robin pairwise comparison, weekly cadence, bias mitigation
   (order-swap, tie policy, human-calibration check).
7. Weekly champion-stats GitHub issue + checkbox-approval Action (quality, cost, back-catalog-cost,
   apply-and-clear).
8. DeepSeek off-peak/batch scheduling preference (§5.3) — confirm V4's off-peak window and whether a
   distinct batch-submission endpoint exists before implementing; wire as a scheduling preference in the
   dispatch coordinator, not a hard requirement.
