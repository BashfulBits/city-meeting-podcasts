# review/28 — LLM-Assisted City & Agenda-Source Discovery

**Maturity: L3 (dev-ready) · ROADMAP R12 — number out of table-position on purpose, per the
no-renumbering convention; sequenced right after R2, before R3 · issues not yet cut**

> **Promoted 2026-07-12 (maintainer request): "push toward L2, research the best answer for each open
> question."** The L1 sketch left two decisions open: which search-grounding mechanism, and whether this
> rides the `InferenceJob` interface or a separate script. Both were researched and decided (§2, §3) —
> the answers converged: the "search" step turns out not to be an LLM call at all, which resolved both
> questions together.
>
> **Matured further to L3, same day (maintainer request: "push further, ask me with anything you're not
> sure of").** Two open decisions — trigger cadence, and what the checkbox-approval Action does — were
> put to the maintainer directly rather than assumed. The answers **expanded scope**: this item now
> explicitly covers new-city bootstrapping (previously deferred as "no existing manual process to
> automate against"), because this repo already has one — the `add-city` issue template (§5.2) — R12
> automates fulfilling it, not a new intake surface. The answers also specified a real, non-trivial
> commit-gating algorithm (§7) rather than a simple "PR vs. direct commit" toggle.

---

## §1. Problem (recap, tightened from the L1 sketch)

R11's §B.2 discovery checklist and Appendix P's platform census (`review/15`) make per-city agenda-source
discovery *tractable* but still fully manual: a human finds the city's real website, clicks through to
whatever portal it links, and matches that against the census by hand. Four guessed-URL failures this
session (`dentontx.legistar.com`, `portal-{org}.primegov.com`, etc.) are exactly the cost this item
exists to remove.

**Two sub-problems, both now in scope, sharing one mechanism:**

- **(a) R11's own scope** — auxiliary agenda-source discovery for cities already in the catalog (the
  original target).
- **(b) New-city bootstrapping** — finding a brand-new city's video provider, agenda provider, and
  meeting bodies from scratch, and proposing a complete new `config/feeds/<slug>.yml`. **Previously
  deferred in the L2 pass as "no existing manual process to automate against" — that assumption was
  wrong.** This repo already has a manual process: the `add-city` GitHub issue template
  (`.github/ISSUE_TEMPLATE/add-city.yml`), whose own description says *"a maintainer will add a
  `config/feeds/<slug>.yml`"* — today, entirely by hand. R12 automates fulfilling that existing promise,
  not a new intake surface (§5.2).

---

## §2. Decision 1 — Search-grounding mechanism: a dedicated search API, not native LLM-provider grounding

**Researched 2026-07-12. Conclusion: use a dedicated search API (Tavily) as the retrieval step, and keep
the LLM call itself an ordinary structured-output request — no provider "grounding"/tool-calling feature
at all.** This is a firm recommendation backed by concrete, current findings, not a coin flip.

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
| **Brave Search API** | **None as of Feb 12, 2026** — free tier eliminated, now $5 prepaid credit (~1,000 queries) then metered, credit card required, mandatory public attribution | Real, recent precedent for "a vendor's free tier can vanish with little warning" — noted as a live risk for whichever option is chosen (§9) |
| **SerpApi** | Conflicting reports (10–100/month depending on source — inconsistent enough to distrust); paid tiers from ~$25–75/month | Also under an active Google DMCA lawsuit (filed Dec 2025, motion to dismiss Feb 2026, hearing scheduled May 2026) — a ToS/legal cloud independent of price, reason enough to avoid regardless of tier details |
| **Tavily** | **1,000 free credits/month, no credit card required**, $0.008/credit pay-as-you-go beyond that. Purpose-built for LLM/RAG grounding — returns structured, chunked results designed to be handed straight to a model | **Recommended.** Matches this project's free-tier-first posture (no card, no billing-account precondition), purpose-fit for exactly this "ground the model in retrieved text" pattern R2 §4 already established for chapter-boundary chunking, and provider-independent (works with whichever of Gemini/DeepSeek/Mistral R2's routing picks, no lock-in to one LLM vendor's search feature) |

**Volume check:** the current catalog has ~10 distinct city entities (`config/feeds/` slugs collapse to
addison, arlington, austin, dallas, denton-county, denton-tx, fort-worth, gainesville, pflugerville,
travis). Even generous future growth to several dozen cities, plus periodic re-checks for platform
migrations (Appendix P's IQM2/NovusAGENDA EOL tripwires) and new-city requests, stays comfortably inside
1,000 free searches/month.

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
  job is," not per-verb code paths** (`review/27` §3.1). They're consulted by task verb, not by Stage
  identity.
- `TASK_VERSIONS`/`TASK_PROMPTS` are plain `dict[Task, ...]` registries — adding a verb is "one `Literal`
  member, one version constant, one prompt template," explicitly designed for exactly this kind of
  extension, with `review/11` §3.5 already reserving `embed`/`translate`/`extract` as precedent for verbs
  that aren't per-episode-audio at all.

**Conclusion: `InferenceJob`/`Backend`/the budget ledger were never Stage-bound in the first place** — a
new, independent caller (a scheduled Action, or a workflow triggered off an issue label — see §5) can
construct an `InferenceJob` and call the LLM `Backend`'s `run_inference()` directly, and gets the budget
ledger + provider routing + versioning "for free."

### §3.2 One verb, not two — `classify-civic-platforms`

**Renamed from the L2 pass's `classify-agenda-source`** to reflect the now-expanded scope: new-city mode
(§1(b)) needs to classify the *video* provider too, not just the agenda provider. Rather than two
near-duplicate task verbs, this is **one verb with a mode-aware prompt**, matching R2 §4.3's "one shared
structured template per task-verb" convention — the template's Context section varies by mode, not the
verb itself:

- `citypods/compute/base.py:29-36` — add `"classify-civic-platforms"` to the `Task` Literal.
- `TASK_VERSIONS["classify-civic-platforms"]` — one version constant.
- `TASK_PROMPTS["classify-civic-platforms"]` — one structured template. **Context** (mode-dependent):
  - *Aux-discovery mode*: city name/state, the city's **already-known** video provider (not up for
    reclassification), Tavily's retrieved search results, Appendix P's agenda-platform census subset.
  - *New-city mode*: city name/state, any hints from the `add-city` issue form (§5.2 — provider dropdown,
    `source_url`, `city_website`, notes — all optional, all treated as hints to verify, never as trusted
    facts, per the same "verify, don't guess" discipline that shaped R11), Tavily's retrieved search
    results, Appendix P's **full** census (video + agenda platforms).
  - **Instructions** (both modes): classify only from what's in the retrieved search results — never
    propose a URL that doesn't appear in the retrieved context, closing off the one channel through which
    recall-based hallucination could still leak in even with real search grounding. Output a "no
    confident match" result rather than a low-confidence guess.
  - **Output schema**: `video_platform` (enum from Appendix P + `null`, aux-discovery mode always
    returns the input's known value unchanged), `agenda_platform` (enum + `null`), `candidate_urls`
    (list, each tied to a specific retrieved search result), `bodies_mentioned` (list of board/commission
    names surfaced in the retrieved results, best-effort), `confidence` (low/medium/high, informational —
    §6 explains why this never gates anything by itself), `reasoning` (one sentence).
- `recipe_hash` = hash of `(city_slug, mode, prompt_version, tavily_result_content)` — a re-run against
  unchanged search results is a free cache hit. Not a hard requirement for a first cut (call volume is
  low enough that caching is a nicety) but cheap to include since the mechanism already exists.

---

## §4. Full pipeline

Both modes share the same five-stage shape; they differ in trigger (§5), search-query scope, verification
depth (§6), and where output is posted (§7).

1. **Search** (not an `InferenceJob` — a plain API call). New module `citypods/discovery/search.py` calls
   Tavily's API with the city name/state (+ any known hints) and gets back structured results (URLs +
   snippets). A direct call to a trusted first-party API host with our own key — not SSRF-gated, the same
   way LiteLLM's own calls to Gemini/DeepSeek/Mistral hosts aren't (the SSRF gate exists to guard fetches
   of *untrusted, externally-supplied* URLs, not our own calls to a known API vendor).
2. **Classify** (`InferenceJob(task="classify-civic-platforms", inputs={"mode": ..., ...})`). One ordinary
   structured-output LLM call — any of Gemini/DeepSeek/Mistral, whichever R2's existing provider-
   allocation policy routes to. No tool-calling, no grounding feature, none of §2.1's bugs are in play.
3. **Verify** (Python, mandatory, never skipped, tiered by mode — §6).
4. **Assemble evidence** (§7) — the bundle a human actually reviews before checking a box.
5. **Propose or reply** (§5/§7) — a GitHub issue/comment with a checkbox. Steps 1–4 never touch config
   directly; checking the box is what triggers §8's apply logic.

---

## §5. Two trigger surfaces

**Resolved 2026-07-12 (maintainer decision, both confirmed): a low-frequency scheduled sweep for
aux-discovery, plus dispatch off the existing `add-city` issue label for new-city requests.** Not a
single mechanism — the two modes have genuinely different triggers because one operates over a small,
known, existing set of cities and the other is inherently on-demand (triggered by whoever files the
request).

### §5.1 Aux-discovery: scheduled sweep + manual dispatch

New workflow, e.g. `.github/workflows/city-discovery.yml`, modeled directly on `availability-digest.yml`'s
shape (`on: schedule` + `workflow_dispatch`, `concurrency` group, network-touching so it's its own job
rather than riding PR CI):

```yaml
on:
  schedule:
    - cron: "0 9 1 */3 *"  # 09:00 UTC on the 1st, every 3 months — quarterly
  workflow_dispatch:
    inputs:
      city_slug:
        description: "Run against one city only (optional; default sweeps every eligible city)"
        required: false
```

**Cadence: quarterly, proposed as a concrete default, tunable.** Reasoning: unlike `availability-digest`'s
continuously-changing withheld-media queue, aux-discovery's target list is small and near-static (§9.1) —
weekly would mostly re-confirm "still nothing new" and burn Tavily/LLM budget for no benefit; quarterly
still catches Appendix P's IQM2/NovusAGENDA EOL migrations (which unfold over months, per the Waco
precedent in `review/15`) well within their runway. Every run is also independently triggerable via
`workflow_dispatch` for an on-demand check.

**Eligible cities for the sweep** (computed at run time, not hardcoded): every `City` whose current
`provider` lacks native agenda data (Swagit, and any Granicus city not already covered by Part A's
migration) **and** whose `aux_provider` is not already set. This makes the sweep self-limiting as R11
Part A/B execution proceeds — cities gain `aux_provider` and drop out of future sweeps automatically.

### §5.2 New-city bootstrapping: dispatch off the existing `add-city` label

**No new intake surface — reuses `.github/ISSUE_TEMPLATE/add-city.yml`, which already exists and already
auto-applies the `add-city` label.** New workflow trigger:

```yaml
on:
  issues:
    types: [opened, labeled]
```

with a job-level `if: contains(github.event.issue.labels.*.name, 'add-city')` — covers both the normal
case (issue opened via the template, label auto-applied) and a maintainer manually labeling an
existing free-form issue to request processing.

The workflow parses the issue-form fields directly (GitHub issue forms produce a parseable structured
body): `city_state`, `provider` (dropdown, `"Not sure"` by default), `source_url`, `city_website`,
`notes`. All are passed to the classification step as **hints to verify, not facts to trust** — the same
posture R11 already established for the maintainer's own recollections (e.g. "Denton uses Legistar,"
unconfirmed until independently verified). Results post as a **reply comment on the same issue**, not a
new digest issue — matching the maintainer's own framing ("the GH issue, or reply if invoked manually on
a new city issue").

**Minor, optional follow-on noted, not blocking this item:** `add-city.yml`'s `provider` dropdown
currently only offers `granicus`/`civicplus`/`other` — Appendix P's full census (Swagit, CivicClerk,
OneMeeting/PrimeGov, etc.) could be added as explicit options. Not required since `"Not sure"`/`"other"`
already route to full discovery either way.

---

## §6. Verification — tiered, and mode-dependent

**Platform-signature verification (both modes, unchanged from the L2 design):** Python live-fetches any
candidate URL through the existing SSRF-gated `make_session()` (`citypods/http.py:264-337` — the same
gate every other fetch path in this codebase already goes through) and confirms it matches a known
platform signature. A candidate that fails is dropped, never proposed. Scoped initially to Appendix P's
"live-verified"/"search-evidenced" tier (§9.3 has the concrete near-term list).

**New for this pass: end-to-end sample-episode verification, required before any config is ever
proposed as apply-able.** The maintainer's evidence requirement ("link to a sample video... proving it
actually works") surfaces a real gap the L2 design didn't have: confirming a portal page *loads* is not
the same as confirming this codebase can actually *resolve playable media* from it. Two cases:

- **The classified provider already has an adapter in this codebase** (Swagit, Granicus, CivicClerk,
  CivicPlus, Legistar/OneMeeting per R11 Parts A–D once shipped): construct the proposed `source` config,
  call that provider's real `fetch_episodes(source)`, take the first returned `Episode`, call
  `resolve_media_url()` on it, and confirm the resolved URL returns HTTP 200 with a plausible
  video/audio content-type via a `HEAD` request (SSRF-gated, same session). This reuses 100% existing
  provider code — no new media-resolution logic, just calling what already exists with a candidate config
  before proposing it. **This is the strongest verification signal available and should gate whether the
  "apply" checkbox is offered at all.**
- **The classified provider has no adapter in this codebase yet** (a genuinely new platform from Appendix
  P's wider census, or something not in the census at all): full end-to-end verification is impossible
  without writing that adapter first, which is out of scope for a discovery task. **The apply/checkbox
  path must be disabled entirely for this case** — the issue/comment reports the finding (website,
  listing page, whatever sample link Tavily's results surfaced) clearly labeled **"research finding only —
  no adapter exists yet, cannot auto-apply"**, so the maintainer gets the discovery value (saved research
  time) without R12 ever proposing a config change it can't actually verify works.

**Confidence is informational, never a gate.** The LLM's self-reported `confidence` field (§3.2) is shown
in the issue for human context; it never decides whether a candidate reaches the human. Verification
pass/fail is the only real gate, per the L2 design's original reasoning (a wrong-but-plausible config is
worse than no config).

---

## §7. Evidence bundle & issue/comment schema

**Every proposal, in both modes, must show — per the maintainer's explicit requirement — before a human
is asked to decide anything:**

1. City website link (from Tavily's search results, independently of anything else found).
2. Video/meeting-listing page link (the classified provider's listing page — e.g. a Granicus
   `ViewPublisher.php` page, a PrimeGov `/public/portal`, a CivicPlus `/AgendaCenter`).
3. **A sample video link** — the actual resolved media URL from §6's end-to-end check, not just a page
   that mentions video exists. Absent entirely (with the "no adapter yet" label) when §6's adapter-exists
   gate fails.
4. Every meeting body/board name discovered (`bodies_mentioned` from §3.2's output schema), so the
   maintainer can see at a glance whether this looks like real, complete coverage or a partial match.
5. The proposed YAML diff itself (new file for new-city mode; `aux_provider`/`aux_source` keys for
   aux-discovery mode).
6. A checkbox: **"- [ ] Apply this configuration"** — present only when §6's verification fully passed
   (platform signature **and** end-to-end sample resolution); absent (replaced by the "research finding
   only" label) otherwise.

**Aux-discovery mode** — one rolling digest issue (title `[city-discovery] N candidate(s) pending`),
mirroring `scripts/audit_feeds.py`'s consolidated-issue/hidden-JSON-state pattern: one section per city,
a hidden `<!-- citypods:city-discovery:state ... -->` JSON block tracking per-city status
(`proposed`/`applied`/`rejected`/`no-match`) across runs so a quarterly re-run only adds/updates sections
that changed, exactly like `audit_feeds.py`'s create/update/close reconciliation loop. Cities with no
confident match get a one-line, no-checkbox mention (visibility without checkbox-noise), not a full
section.

**New-city mode** — a reply **comment** on the originating `add-city` issue (not a new issue), containing
the same evidence bundle scoped to that one city, with its own checkbox. Once applied (§8), the workflow
labels the issue `add-city:applied` and closes it, mirroring this project's general open-while-unresolved/
auto-close-on-resolution issue lifecycle convention (H4's audit issues).

---

## §8. Apply mechanism — commit-gating algorithm

**Resolved 2026-07-12 (maintainer decision): commit directly to main when checked, gated so a direct
commit is only ever allowed when the change is purely additive — nothing in the repository is ever
modified or deleted by this path. Falls back to opening a PR otherwise.**

### §8.1 This repo's real branch rules, verified live (not assumed from a code comment)

`lock.yml`'s own comment claims *"branch protection blocks main."* **Checked directly against the live
repo, 2026-07-12** (`gh api repos/.../branches/main/protection` → 404 "Branch not protected"; the modern
Rulesets API, `gh api repos/.../rulesets`, shows one active ruleset named `main` whose only two rules are
`deletion` and `non_fast_forward` — no required-PR rule, no required-status-checks rule, `bypass_actors:
[]`). **`lock.yml`'s comment is stale relative to the live ruleset** — a normal fast-forward push to
`main` with a `contents: write` token is not actually blocked today. This is exactly the kind of claim
this whole item's discipline says to verify rather than trust, and it's the reason a genuine direct-commit
design (not a PR-with-auto-merge workaround) is achievable here.

### §8.2 The additivity check

Before any commit, re-fetch a fresh `main` checkout (guards against staleness between proposal-time and
checkbox-click-time) and classify the change:

- **New-city mode**: purely additive **iff** `config/feeds/<slug>.yml` does not already exist at the
  fresh HEAD (and, if a new `config/cities/<slug>.yml` entity is also being created, that path doesn't
  exist either). Any existing file at either path ⇒ not additive.
- **Aux-discovery mode**: purely additive **iff**, parsing the existing `config/feeds/<slug>.yml` fresh,
  the `aux_provider` key is currently absent (never overwrite an already-set value, whether set by a
  human or a prior R12 run). The patch inserts only the new `aux_provider`/`aux_source` keys; every
  pre-existing key/value in the file is asserted byte-identical before and after.

**Redundant backstop, regardless of mode:** stage only the exact target path(s) — never `git add -A` —
and run `git diff --cached --stat` immediately before committing to assert **zero deletions and zero
modified-line-count on any pre-existing file**, matching the maintainer's literal requirement ("ensuring
that nothing in the repository is deleted"). Any failure of this assertion aborts the direct-commit path
even if the earlier structural check passed — belt-and-suspenders, not redundant paranoia, given this is
the first automation in this codebase with write access to `main`.

### §8.3 Two paths

- **Additive ⇒ direct commit.** Stage the exact new/target file(s), commit (message references the
  source issue/comment, e.g. `config: add aux_provider for gainesville-tx (via #1234, R12 discovery)`,
  attributed to an automation identity), `git push origin HEAD:main`. Reply on the issue/comment
  confirming the commit SHA.
- **Not additive ⇒ PR fallback.** Push a branch, open a PR carrying the identical diff, and say so
  explicitly in a reply ("this would modify an existing value, opening a PR for review instead of
  committing directly — see #<PR>") so the maintainer isn't left wondering why nothing happened to `main`.

### §8.4 Credential handling — mirrors `lock.yml`'s existing, already-trusted pattern exactly

`lock.yml` is this repo's only existing `contents: write` workflow and already establishes the right
pattern: `persist-credentials: false` on checkout, the write-capable token introduced only at the final
push step. R12's Action copies this verbatim rather than inventing a new credential-handling approach.

---

## §9. New dependency, and risks

### §9.1 Tavily (new dependency)

A genuinely new kind of dependency for this codebase — every existing external integration is either an
LLM provider (via LiteLLM) or a civic-data provider (Legistar/Granicus/Swagit/CivicClerk/CivicPlus);
Tavily is the first pure search-vendor dependency.

- Requires a Tavily API key (free signup, no card) — one new secret, `TAVILY_API_KEY`, following this
  repo's existing `{SERVICE}_{FIELD}` secret-naming convention (e.g. `HF_TOKEN`, `B2_KEY_ID`).
- **Vendor risk is real, not hypothetical — Brave's free-tier cut on 2026-02-12 is a concrete precedent.**
  Mitigation: this feature is a pure productivity multiplier on top of already-working manual processes
  (R11's §B.2 checklist; the `add-city` template's manual fulfillment) — not a hard dependency for
  anything else in the catalog. If Tavily's free tier disappears, R12 stops running and both surfaces
  fall back to their existing manual paths, a regression to today's status quo, not a break.

### §9.2 Risks

- **First automation in this codebase with `contents: write` on `main`.** Every prior automation
  (`audit_feeds.py`, `availability-digest.yml`, R2's champion-routing ticket) only ever writes GitHub
  issues — this is a materially higher-trust capability, worth stating plainly rather than downplaying.
  Mitigated by §8's additivity gate + redundant diff-stat backstop + `lock.yml`'s proven credential
  pattern, but the risk class itself (a bug in the gating logic could, in principle, commit something
  unintended) is new to this project and should be reviewed with that in mind before shipping.
- **Vendor free-tier risk** (§9.1) — bounded by the graceful-degradation posture above.
- **Census incompleteness** — Appendix P is "exhaustive" for platforms found by research this session,
  not a closed set; the classification prompt has a clean "no confident match" output (§3.2) rather than
  forcing a guess, falling through to manual discovery.
- **Classification errors survive despite good retrieval** — an LLM can still misclassify or hallucinate
  a plausible-but-wrong URL even when given real search results as context. This is exactly why §6's
  two-tier verification (platform signature **and** end-to-end sample resolution) is mandatory, not a
  nice-to-have.
- **SSRF discipline must actually be followed at every verification fetch** — candidate URLs are
  LLM-proposed, i.e. indirectly externally-influenced, so every one goes through `make_session()` like
  every other externally-sourced URL this codebase fetches.
- **Per-platform verifier functions are real work, not a detail** (§9.3) — as many small
  `verify_{platform}(url) -> bool` functions as census entries this actually tries against.
- **New-city mode's provider hints (from the `add-city` form) must never be trusted uncritically** —
  treated as search-disambiguation hints only, verified the same way as anything else (§3.2), matching
  R11's own "maintainer recollection ≠ confirmed fact" discipline.

### §9.3 Near-term verifier/adapter targets (corrected from the L2 pass)

The L2 draft listed Travis County as a "near-term target" — **wrong, corrected here.** Travis County
already runs `provider: civicclerk` **natively** as its primary provider (`config/feeds/travis-county-tx.yml`
confirmed live) — it needs no auxiliary discovery at all. The real aux-discovery targets, grounded
against the live catalog:

- **The 4 Swagit cities** (`addison-tx`, `austin-tx`, `dallas-tx`, `denton-tx`) — Swagit has no native
  agenda data at all, so these are the primary gap. Check order per `review/15` §B.2: swagit-attachments
  shortcut first, then Legistar/OneMeeting/Agenda-PE.
- **Gainesville** (`provider: civicplus`) — the concrete Part D target; R12 discovers Gainesville's real
  CivicClerk tenant hostname (pattern confirmed live at Travis County's own config,
  `traviscotx.api.civicclerk.com` — Gainesville's tenant must still be *discovered*, never guessed from
  that pattern).
- **The 4 Granicus cities** (`arlington-tx`, `denton-county`, `fort-worth-tx`, `pflugerville-tx`) —
  secondary/stretch target; Part A's Legistar migration already has an L3 plan for these, so R12's sweep
  naturally excludes any that already gain `aux_provider` through that execution (§5.1's self-limiting
  eligibility check).

Verifier functions needed for this scope: `verify_legistar` (Calendar.aspx returns real rows, not
"Invalid parameters!"), `verify_primegov` (JSON API returns non-empty `documentList[]`), `verify_civicclerk`
(reuses Part D's existing `fetch_agenda_index` directly — no second verifier needed), `verify_civicengage`
(`/AgendaCenter` returns expected markup), `verify_swagit_attachments` (per `review/15` §B.2's shortcut).
Agenda PE needs no distinct verifier — Appendix P found it publishes through standard
`{org}.granicus.com` pages the existing Granicus verifier already covers.

---

## Proposed GitHub issues (not filed — batch review pending)

1. `citypods/discovery/search.py` — Tavily client wrapper (new dependency, new `TAVILY_API_KEY` secret).
2. `Task` Literal + `TASK_VERSIONS`/`TASK_PROMPTS` additions for `classify-civic-platforms`
   (`citypods/compute/base.py`, LLM-adjacent module per `review/27`'s module plan), mode-aware prompt
   per §3.2.
3. Per-platform verifier functions scoped to §9.3's near-term list, plus the end-to-end
   sample-episode-resolution check (§6) reusing each provider's existing `fetch_episodes`/
   `resolve_media_url`.
4. `.github/workflows/city-discovery.yml` — aux-discovery sweep (schedule + `workflow_dispatch`),
   modeled on `availability-digest.yml`; rolling digest issue with hidden-state reconciliation per §7.
5. New-city workflow triggered off the existing `add-city` label (§5.2); reply-comment posting, issue
   labeling/close-on-apply lifecycle.
6. The shared commit-gating Action (§8) — additivity check, diff-stat backstop, direct-commit and
   PR-fallback paths, credential handling mirroring `lock.yml`.
7. Optional, non-blocking: extend `add-city.yml`'s `provider` dropdown with Appendix P's fuller platform
   list (§5.2).
