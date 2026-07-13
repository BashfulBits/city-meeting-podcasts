# review/28 — LLM-Assisted City & Agenda-Source Discovery

**Maturity: L2 (chosen approach, breakout of [`review/11`](11-technical-design-roadmap.md) §5.1) ·
ROADMAP R12 — number out of table-position on purpose, per the no-renumbering convention; sequenced
right after R2, before R3 · issues not yet cut**

> **Promoted 2026-07-12 (maintainer request): "push toward L2, research the best answer for each open
> question."** The L1 sketch left two decisions open: which search-grounding mechanism, and whether this
> rides the `InferenceJob` interface or a separate script. Both are now researched and decided below —
> the answers changed the architecture in one real way (see §3): the "search" step turns out not to be
> an LLM call at all, which resolves both open questions together rather than independently.

---

## §1. Problem (recap, tightened from the L1 sketch)

R11's §B.2 discovery checklist and Appendix P's platform census (`review/15`) make per-city agenda-source
discovery *tractable* but still fully manual: a human finds the city's real website, clicks through to
whatever portal it links, and matches that against the census by hand. Four guessed-URL failures this
session (`dentontx.legistar.com`, `portal-{org}.primegov.com`, etc.) are exactly the cost this item
exists to remove. Two sub-problems, kept distinct per the L1 sketch: (a) R11's own scope — auxiliary
agenda-source discovery for catalog cities; (b) a broader scope R11 doesn't cover — bootstrapping a
brand-new city's *video*-provider config from scratch. Both share the mechanism below.

---

## §2. Decision 1 — Search-grounding mechanism: a dedicated search API, not native LLM-provider grounding

**Researched 2026-07-12. Conclusion: use a dedicated search API (Tavily) as the retrieval step, and keep
the LLM call itself an ordinary structured-output request — no provider "grounding"/tool-calling feature
at all.** This is a firm recommendation backed by concrete, current findings, not a coin flip:

### §2.1 Why native Gemini grounding doesn't fit

- **Free-tier grounding is model-restricted and about to get worse.** Gemini 2.5 Flash gets 500
  requests/day free grounding, then $35/1,000 grounded prompts. **Gemini 3 models (3.5 Flash, 3.1
  Flash-Lite) have *no* free-tier grounding at all** — the "5,000 prompts/month free" allocation Google
  advertises only exists on the *paid* tier (a billing account must be attached even to stay under that
  free monthly quota), then $14/1,000 search queries. R2 already committed to Gemini's genuinely free,
  no-billing-account tier as the primary daily driver with a hard stop at quota (`review/27` §5) — native
  grounding would either lock this item to the older 2.5 Flash model specifically, or require attaching
  real billing to the Gemini account, a materially different posture than R2's current design.
- **LiteLLM's grounding support has real, current structural bugs, not just rough edges.** Two confirmed
  this pass: (1) Vertex AI/Gemini **cannot mix a hosted search tool with function-calling/structured-output
  tools in the same request** — a platform-level API constraint, not a LiteLLM bug, so LiteLLM silently
  drops the search tool whenever both are present in one call ([BerriAI/litellm#27479](https://github.com/BerriAI/litellm/issues/27479)).
  R12's actual need is exactly this combination (search *and* return a structured classification) — so
  native grounding would force an awkward two-call pattern even before considering (2): LiteLLM currently
  drops `grounding_metadata` (the citation URLs the model actually found) from the response entirely
  ([BerriAI/litellm docs/issue threads](https://docs.litellm.ai/docs/completion/web_search)), which is the
  one piece of information this feature can't function without.
- **DeepSeek's native web search isn't reachable through R2's existing integration path.** DeepSeek does
  have a web-search feature, but it's documented as available specifically through DeepSeek's
  Anthropic-Messages-compatible endpoint (`server_tool_use`/`web_search_tool_result`), not the standard
  OpenAI-compatible endpoint R2's `deepseek/...` LiteLLM routing already uses. Unconfirmed whether
  LiteLLM bridges this; not worth the integration risk when an alternative exists.
- **Mistral has no native search tool at all** — confirmed via Mistral's own capability docs; third-party
  layers (e.g. Opper) exist to bolt one on, which just reintroduces the "new dependency" question §2.2
  answers more directly anyway.

### §2.2 Why a dedicated search API, and why Tavily specifically

Evaluated four options against this project's established free-tier-first, no-mandatory-billing
discipline:

| Option | Free tier (2026-07-12) | Verdict |
|---|---|---|
| **Bing Search API** | None — **fully retired August 11, 2025**, public endpoints return HTTP 410. Not a live option at all | Dead; confirming this in research (rather than assuming from training-data recall) is itself the kind of "verify, don't guess" mistake this whole item exists to prevent |
| **Brave Search API** | **None as of Feb 12, 2026** — free tier eliminated, now $5 prepaid credit (~1,000 queries) then metered, credit card required, mandatory public attribution | Real, recent precedent for "a vendor's free tier can vanish with little warning" — noted as a live risk for whichever option is chosen (§7) |
| **SerpApi** | Conflicting reports (10–100/month depending on source — inconsistent enough to distrust); paid tiers from ~$25–75/month | Also under an active Google DMCA lawsuit (filed Dec 2025, motion to dismiss Feb 2026, hearing scheduled May 2026) — a ToS/legal cloud independent of price, reason enough to avoid regardless of tier details |
| **Tavily** | **1,000 free credits/month, no credit card required**, $0.008/credit pay-as-you-go beyond that. Purpose-built for LLM/RAG grounding — returns structured, chunked results designed to be handed straight to a model | **Recommended.** Matches this project's free-tier-first posture (no card, no billing-account precondition), purpose-fit for exactly this "ground the model in retrieved text" pattern R2 §4 already established for chapter-boundary chunking, and provider-independent (works with whichever of Gemini/DeepSeek/Mistral R2's routing picks, no lock-in to one LLM vendor's search feature) |

**Volume check:** the current catalog has ~10 distinct city entities (`config/feeds/` slugs collapse to
addison, arlington, austin, dallas, denton-county, denton-tx, fort-worth, gainesville, pflugerville,
travis). Even generous future growth to several dozen cities, plus periodic re-checks for platform
migrations (Appendix P's IQM2/NovusAGENDA EOL tripwires), stays comfortably inside 1,000 free
searches/month.

**Consequence for the architecture (§3): the search step is not an LLM call.** Tavily returns real search
results (URLs + snippets) directly — no model involved yet. Classification against Appendix P's census
happens in a *separate*, ordinary LLM call fed those results as retrieved context, with no tool-calling or
provider-native grounding feature needed anywhere in the pipeline. This sidesteps every bug/limitation in
§2.1 simultaneously, rather than working around each one.

---

## §3. Decision 2 — Architecture: reuse `InferenceJob` for classification only; the search step lives outside it

**Researched 2026-07-12, grounded directly in the codebase.** The L1 sketch framed this as "InferenceJob
vs. a separate script." That framing turned out to be a false dichotomy once §2's finding is factored in
— the real answer is **use `InferenceJob` for the one piece of this that's actually LLM inference (the
classification call), and treat search as a plain, non-inference API call outside that interface
entirely.**

### §3.1 What grounding in the codebase confirmed

- `Backend.run_inference(job: InferenceJob) -> JobResult | JobHandle` (`citypods/compute/base.py:93-103`)
  is a plain Protocol — it takes a task verb + inputs + recipe hash and returns an artifact. Nothing in
  its signature assumes an `Episode`.
- The one place this project calls it today is deep inside `TranscriptStage`'s per-episode threaded
  execution (`citypods/stages.py:2679-2709`) — tightly coupled to audio paths, city config, and threading
  primitives specific to ASR. **That call site is not something R12 would plug into** — it's
  transcribe/align-specific machinery, irrelevant to a one-time city-classification task.
- But the things R12 actually wants to reuse — the budget ledger and the tournament/champion-routing
  logic (`review/27` §8, §6) — are **already designed to operate generically "over whichever task this
  job is," not per-verb code paths** (`review/27` §3.1: *"The budget ledger (§8) and tournament (§6)
  already operate generically over 'whichever task this job is.'"*). They're consulted by task verb, not
  by Stage identity.
- `TASK_VERSIONS`/`TASK_PROMPTS` are plain `dict[Task, ...]` registries (`review/27` §3.1) — adding a verb
  is "one `Literal` member, one version constant, one prompt template," explicitly designed for exactly
  this kind of extension, with `review/11` §3.5 already reserving `embed`/`translate`/`extract` as
  precedent for verbs that aren't per-episode-audio at all.

**Conclusion: `InferenceJob`/`Backend`/the budget ledger were never Stage-bound in the first place — the
Stage coupling is specific to *how ASR happens to call it today*, not a property of the interface.** A new,
independent caller (a CLI command or a scheduled Action script, not a Stage) can construct an
`InferenceJob` and call the LLM `Backend`'s `run_inference()` directly, and gets the budget ledger +
provider routing + versioning "for free" — no new plumbing to duplicate, which was the concern the
"separate script" alternative was trying to avoid in the first place.

### §3.2 The new verb

One new `Task` Literal member: **`"classify-agenda-source"`**. Per §3.1's extension recipe:

- `citypods/compute/base.py:29-36` — add `"classify-agenda-source"` to the `Task` Literal.
- `TASK_VERSIONS["classify-agenda-source"]` — one version constant.
- `TASK_PROMPTS["classify-agenda-source"]` — one structured template (Context: city name/state, existing
  config, Tavily's retrieved search results; Instructions: classify against Appendix P's census;
  Output: candidate platform + candidate URL(s) + confidence + one-line reasoning). Appendix P's census
  table is the grounding context handed to the model — the same "ground in retrieved text, don't rely on
  recall" principle R2 §4 already established for transcript chunking, applied here to platform
  knowledge instead of meeting content.
- `recipe_hash` = hash of `(city_slug, prompt_version, tavily_result_content)` — makes a re-run against
  unchanged search results a free cache hit rather than a re-spent LLM call, the same content-addressing
  discipline every other verb already uses. Not a hard requirement for a first cut (call volume is low
  enough that caching is a cost nicety, not a necessity) but cheap to include since the mechanism already
  exists.

### §3.3 Full pipeline (supersedes the L1 sketch's 4-step mechanism)

1. **Search (not an `InferenceJob` — a plain API call).** New small module, e.g.
   `citypods/discovery/search.py`, calls Tavily's API with the city name/state (+ any existing entity
   metadata for disambiguation) and gets back structured results (URLs + snippets). This is a direct call
   to a trusted first-party API host with our own key — not SSRF-gated, the same way LiteLLM's own calls
   to Gemini/DeepSeek/Mistral hosts aren't (the SSRF gate exists to guard fetches of *untrusted,
   externally-supplied* URLs, not our own calls to a known API vendor).
2. **Classify (`InferenceJob(task="classify-agenda-source", ...)`).** One ordinary structured-output LLM
   call — any of Gemini/DeepSeek/Mistral, whichever R2's existing provider-allocation policy routes to —
   fed Tavily's real search results plus Appendix P's census as context. No tool-calling, no grounding
   feature, none of §2.1's bugs are in play. Output: a candidate platform + URL(s) + confidence.
3. **Verify (Python, mandatory, never skipped).** Live-fetch the candidate URL through the existing
   SSRF-gated `make_session()` (`citypods/http.py:264-337` — the same gate every other fetch path in this
   codebase already goes through) and confirm it matches a known platform signature. Start only against
   Appendix P's "live-verified"/"search-evidenced" tier (formalizing a verifier per platform is real,
   non-trivial follow-on work — §8). A candidate that fails verification is dropped, never proposed.
4. **Propose (GitHub issue + checkbox, never auto-applied).** One issue per city (or a batched digest,
   mirroring `scripts/audit_feeds.py`'s consolidated-issue/hidden-JSON-state reconciliation pattern) with
   the proposed YAML diff and the verification evidence, wired to the same checkbox-parsing Action R2 §6.3
   already designs for champion routing. Checking the box is what writes the YAML; steps 1–3 never touch
   config directly.

---

## §4. New dependency: Tavily

A genuinely new kind of dependency for this codebase — every existing external integration is either an
LLM provider (via LiteLLM) or a civic-data provider (Legistar/Granicus/Swagit/CivicClerk/CivicPlus);
Tavily is the first pure search-vendor dependency. Flagged explicitly, not smuggled in as "just another
API call":

- Requires a Tavily API key (free signup, no card) — one new secret alongside existing LLM provider keys.
- **Vendor risk is real, not hypothetical — Brave's free-tier cut on 2026-02-12 is a concrete precedent
  for exactly this happening to a search API with little warning.** Mitigation: this feature is a pure
  productivity multiplier on top of R11's already-working manual §B.2 process, not a hard dependency for
  anything else in the catalog. If Tavily's free tier ever disappears or gets prohibitively metered, R12
  simply stops running and every city falls back to manual discovery — a regression to today's status
  quo, not a break.

---

## §5. Risks

- **Vendor free-tier risk** (§4) — bounded by the graceful-degradation posture above.
- **Census incompleteness** — Appendix P is "exhaustive" for platforms found by research this session,
  not a closed set; the classification prompt should have a clean "no confident match" output rather than
  forcing a guess, falling through to manual §B.2 discovery.
- **Classification errors survive despite good retrieval** — an LLM can still misclassify or hallucinate
  a plausible-but-wrong URL even when given real search results as context. This is exactly why step 3's
  live verification is mandatory and non-negotiable, not a nice-to-have: the design assumes the
  classification step *will* sometimes be wrong, and catches it before a human ever sees a false proposal.
- **SSRF discipline must actually be followed at the one fetch that needs it** (step 3) — the candidate
  URL is LLM-proposed, i.e. indirectly externally-influenced, so it must go through `make_session()` like
  every other externally-sourced URL this codebase fetches; skipping this for "just a verification check"
  would be the one way this feature could reintroduce a real security gap.
- **Per-platform verifier functions are real work, not a detail** — as many small `verify_{platform}(url)
  -> bool` functions as census entries this actually tries against, each needing its own signature
  definition. Scope narrowly at first (§8).

---

## §6. Remaining gap to L3

This is a chosen architecture, not yet dev-ready. Before L3:

1. Firm up the exact `TASK_PROMPTS["classify-agenda-source"]` template (Context/Instructions/Output-format
   sections, per R2 §4.3's established structured-prompt convention).
2. Design the per-platform verifier functions for at least the catalog's near-term targets (Appendix P
   §P.7 item 4: Gainesville→CivicEngage Agenda Center, Travis County→already-built Part D, the Granicus
   cities→already-built Part A, the Swagit cities→the swagit-attachments shortcut first).
3. Design the GitHub issue schema and confirm it can share machinery with R2 §6.3's checkbox-parsing
   Action rather than needing a second, near-duplicate Action.
4. Decide the (a)-vs-(b) scope question from the L1 sketch concretely: build against R11's auxiliary-
   discovery case first (existing cities, existing config schema to target), defer new-city
   video-provider bootstrapping (no existing manual process to automate against yet, a real added-scope
   question — this doc is silent on it, matching the L1 sketch's own scoping choice).
5. Module/file plan and test plan, matching the depth review/15/review/27 already reached for R11/R2.

---

## Proposed GitHub issues (not filed — batch review pending)

1. `citypods/discovery/search.py` — Tavily client wrapper (new dependency, new secret).
2. `Task` Literal + `TASK_VERSIONS`/`TASK_PROMPTS` additions for `classify-agenda-source`
   (`citypods/compute/base.py`, LLM-adjacent module per `review/27`'s module plan).
3. Per-platform verifier functions, scoped initially to Appendix P's "live-verified"/"search-evidenced"
   tier and this catalog's near-term targets (§6 item 2).
4. GitHub issue template + checkbox-parsing Action, sharing machinery with R2 §6.3 where possible.
5. New-city (video-provider) bootstrapping scope decision — a follow-on design pass, not this doc's
   remit, once (or if) the R11-auxiliary-discovery case above is proven out.
