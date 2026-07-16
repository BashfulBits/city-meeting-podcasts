# review/28 — LLM-Assisted City & Agenda-Source Discovery

**Maturity: L3 (dev-ready; implementation in progress, unmerged) · ROADMAP R12 — number out of
table-position on purpose, per the no-renumbering convention; sequenced right after R2, before R3**

> **Promoted 2026-07-12 (maintainer request): "push toward L2, research the best answer for each open
> question."** The L1 sketch left two decisions open: which search-grounding mechanism, and whether this
> rides the `InferenceJob` interface or a separate script. Both were researched and decided (§2, §3) —
> the answers converged: the "search" step turns out not to be an LLM call at all, which resolved both
> questions together.
>
> **Matured further to L3, same day (maintainer request: "push further, ask me with anything you're not
> sure of").** Two open decisions — trigger cadence, and what the approval Action does — were
> put to the maintainer directly rather than assumed. The answers **expanded scope**: this item now
> explicitly covers new-city bootstrapping (previously deferred as "no existing manual process to
> automate against"), because this repo already has one — the `add-city` issue template (§5.2) — R12
> automates fulfilling it, not a new intake surface. The answers also specified a real, non-trivial
> approval and batching workflow (§10) rather than a direct-to-main path. The maintainer subsequently
> chose one permission model: approved proposals are bundled into a maintainer-reviewed PR; R12 never
> commits directly to main.

---

## Implementation handoff — 2026-07-15

The in-progress implementation maps this design to the discovery package, three R12 Actions
workflows, and the city-request intake Worker. It is deliberately not described as shipped until the
implementing PR is merged. The operational contract is:

- Discovery runs daily; auxiliary eligibility is evaluated Monday/weekly unless manually dispatched.
  New-city evidence is posted only on first processing, explicit recheck, or 90-day expiry.
- Approval binds to the exact bot-authored evidence digest. The auxiliary digest requires a city slug;
  neither command can write configuration.
- The scheduled/manual batch recreates one automation branch from fresh main, validates every source
  artifact, stages exact paths only, and opens or updates one maintainer-review PR. The merged-PR
  workflow reconciles source issues and closes only completed Add city issues.
- Research-only provider assignments join the pending-providers tracker through that same review-PR
  path; deferred or assigned cities stay out of weekly auxiliary noise while retaining evidence.
- Before enabling the Actions, create r12:approved, r12:batched, r12:evidence-ready, r12:expired,
  r12:recheck, r12:rejected, needs:provider, and needs:more-information labels. The labels and Tavily/LLM Action secrets are
  configured. The intake Worker is deployed at
  `https://citypods-city-request-intake.citypods.workers.dev`, backed by the provisioned D1 database,
  GitHub App, Formspark webhook, Discord webhook, Turnstile validation, and Resend sender.
- The static `/request-a-city/` page posts to Formspark. Formspark owns the one-time Turnstile token
  verification, then calls the Worker's unguessable `/formspark/<secret>` path. The Worker acknowledges
  within Formspark's two-second/no-retry webhook window and completes D1/GitHub/Discord/Resend work via
  `ctx.waitUntil()`. GitHub API calls carry an explicit `User-Agent`; omitting it produced an edge-level
  empty `403` during live testing.
- R12's non-secret LLM route is task-scoped under `city_discovery` in `config/site_config.yml`.
  Provider API keys remain GitHub Secrets; generic repository Actions variables do not define an
  accidental model policy for future summary, tagging, or soundbite tasks.
- Community/lifecycle code uses the same canonical issue: a signed Discord `/request-city` interaction
  and a `City requests` Discussion create/link that issue; workflow-emitted status events then update
  the stored Discord webhook message, website requester's email, and originating Discussion. Enabling
  these paths still requires the documented Discord application, D1 migration, status secret, two
  source labels, Discussion category, deployment, and controlled live tests.

The email template module includes branded HTML plus complete plaintext variants for acknowledgement,
evidence-ready, review, applied, missing-information, research-only, and expiry states. Lifecycle
notification delivery beyond the initial acknowledgement is enabled through the R12 status callback;
the future website-design phase may refine the shared visual tokens without changing the plaintext fallback.

### Clarification hold for unconfirmed city identity — 2026-07-15

For a new-city request, evidence must confirm the requested municipality before it can be a proposal
or a research-only provider finding. If Tavily results identify another city, or cannot establish the
requested city and state, R12 records no provider gap, adds `needs:more-information`, removes
`needs:discovery`, and pauses scheduled discovery indefinitely. The issue asks for the official city
website, a meeting/video page, or a corrected city/state. `/r12 recheck` explicitly clears that hold
after the requester or maintainer supplies better information. This prevents a misspelling or
underspecified small town from repeatedly consuming discovery capacity or polluting the provider backlog.

### Completion checklist — required before R12 is shipped

- [x] Tavily retrieval, constrained LLM classification, SSRF-safe live verification, evidence rendering,
  90-day refresh, coverage eligibility, provider backlog, and maintainer-review PR batching.
- [x] Deployed Formspark webhook Worker, private D1 contact/dedup store, GitHub App issue creation,
  Discord notification, initial acknowledgement email, and static `/request-a-city/` form.
- [ ] Discord intake: a community request must create/link the canonical GitHub issue without exposing
  requester contact details.
- [ ] Discord progress callbacks: evidence-ready, research-only, batched, applied, and expiry events
  must update the originating Discord thread/channel.
- [ ] GitHub Discussions intake and canonical-issue linking, with the same status callback model.
- [ ] Lifecycle email delivery: wire the prepared templates to GitHub/PR webhook events and the private
  D1 requester record; the initial Worker receipt alone is insufficient.
- [x] Create the R12 GitHub labels.
- [x] Configure Tavily and LLM Action secrets.
- [x] Provision and deploy the Worker: Cloudflare account/project, D1 database, Turnstile widget,
  GitHub App, Formspark form/webhook, Discord webhook, Resend sender domain and DNS records.
- [x] Run a controlled website submission end to end: Formspark/Turnstile → Worker/D1 → GitHub issue
  → Discord notification → Resend receipt (test issue #926, closed and private test row removed).
- [ ] Run the remaining live end-to-end tests: Discord request, Discussion request, verified proposal,
  batch PR, merge, and all outbound status notifications.
- [ ] Optional follow-on: broaden the existing add-city provider dropdown using Appendix P.

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
| **Brave Search API** | **None as of Feb 12, 2026** — free tier eliminated, now $5 prepaid credit (~1,000 queries) then metered, credit card required, mandatory public attribution | Real, recent precedent for "a vendor's free tier can vanish with little warning" — noted as a live risk for whichever option is chosen (§11) |
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
    §6 explains why this never gates anything by itself), `reasoning` (one sentence). The task declares
    this once as a Pydantic response model. The shared LiteLLM backend delegates structured calls to
    Instructor, which derives provider-native schema/JSON mode, validates the typed object, and gives a
    direct call one corrective retry. DeepSeek's public chat route still receives JSON-object mode rather
    than a native schema constraint. The R10 Worker durably carries the Pydantic-generated response
    format; reconciliation validates its completed response against the same named model. A queued
    validation failure is deferred rather than re-asked until the Worker has a durable validation-retry
    transition. This preserves cacheable chunk jobs and leaves batching/off-peak scheduling to the
    dispatch coordinator, not to a task schema.
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
5. **Propose or reply** (§5/§7) — a GitHub issue/comment with evidence and an approval control. Steps 1–4
   never touch config directly; maintainer approval queues the proposal for the next batch PR. No R12
   path commits directly to `main`.

---

## §5. Two trigger surfaces

**Resolved 2026-07-14 (maintainer decision): one daily scheduled workflow plus manual dispatch.** Do not
trigger discovery directly from every issue edit: `issues.edited` is noisy and can loop on bot comments,
labels, and evidence updates. The daily run gives queued R2/Tavily work an overnight window;
`workflow_dispatch` is the maintainer fast path. Both use the same idempotent queue/state logic. The two
modes still have different queue eligibility: new-city work is request-driven, while auxiliary work is
coverage- and `next_check_at`-driven.

### §5.1 Aux-discovery: scheduled sweep + manual dispatch

New workflow, e.g. `.github/workflows/city-discovery.yml`, modeled directly on `availability-digest.yml`'s
shape (`on: schedule` + `workflow_dispatch`, `concurrency` group, network-touching so it's its own job
rather than riding PR CI):

```yaml
on:
  schedule:
    - cron: "0 3 * * *"  # 03:00 UTC / 22:00 Central — daily overnight run
  workflow_dispatch:
    inputs:
      city_slug:
        description: "Run one city only (optional)"
        required: false
      mode:
        description: "all, new-city, auxiliary, batch"
        required: false
```

**Cadence: daily, with weekly auxiliary eligibility.** The daily workflow processes newly queued
website/community/add-city requests, runs discovery, posts evidence, and batches approved proposals
into a PR. This provides the intended overnight turnaround while permitting a maintainer to dispatch
the same workflow immediately when needed. Auxiliary discovery is evaluated weekly within the daily
workflow using persisted `next_check_at` state; a city can be deliberately deferred for a three-month
recheck. The former quarterly-only cadence is superseded.

<!--
**Former cadence rationale:** unlike `availability-digest`'s
continuously-changing withheld-media queue, aux-discovery's target list is small and near-static (§11.1) —
weekly would mostly re-confirm "still nothing new" and burn Tavily/LLM budget for no benefit; quarterly
still catches Appendix P's IQM2/NovusAGENDA EOL migrations (which unfold over months, per the Waco
precedent in `review/15`) well within their runway. Every run is also independently triggerable via
`workflow_dispatch` for an on-demand check.
-->

**Eligible cities for the sweep** (computed at run time, not hardcoded): every `City` whose current
`provider` lacks native agenda data (Swagit, and any Granicus city not already covered by Part A's
migration) **and** whose `aux_provider` is not already set. This makes the sweep self-limiting as R11
Part A/B execution proceeds — cities gain `aux_provider` and drop out of future sweeps automatically.

Before search, measure native agenda coverage over the trailing 365 days. A city with at least five
recent meetings enters `agenda-covered` at **≥95% verified agenda coverage** and is excluded from
auxiliary discovery. It leaves that state only below 90% for two consecutive checks or by explicit
maintainer request. Cities below the five-meeting minimum remain eligible rather than being declared
covered from a tiny sample:

```text
agenda_coverage = meetings_with_verified_agenda_links / meetings_with_expected_agenda_links
```

Persist the numerator, denominator, ratio, measurement time, and evidence source. Other discovery
states are `eligible`, `known-no-agenda`, `assigned-unsupported-provider`, `verified`, `needs-discovery`,
and `rejected`.

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
"live-verified"/"search-evidenced" tier (§11.3 has the concrete near-term list).

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
6. An approval control: `/r12 approve` is available only when §6's verification fully passed (platform
   signature **and** end-to-end sample resolution). Approval queues the proposal for the next batch PR;
   it never writes configuration directly. Unsupported or unverified findings show the "research finding
   only" label instead.

**Aux-discovery mode** — one rolling digest issue (title `[city-discovery] N candidate(s) pending`),
mirroring `scripts/audit_feeds.py`'s consolidated-issue/hidden-JSON-state pattern: one section per city,
a hidden `<!-- citypods:city-discovery:state ... -->` JSON block tracking per-city status
(`proposed`/`approved`/`batched`/`applied`/`rejected`/`no-match`/`expired`) across runs so the daily
workflow only adds/updates sections
that changed, exactly like `audit_feeds.py`'s create/update/close reconciliation loop. Cities with no
confident match get a one-line, no-proposal mention (visibility without proposal noise), not a full
section.

**New-city mode** — a reply **comment** on the originating `add-city` issue (not a new issue), containing
the same evidence bundle scoped to that one city, with its own approval control. Once included in a
merged batch PR (§10), the workflow
labels the issue `add-city:applied` and closes it, mirroring this project's general open-while-unresolved/
auto-close-on-resolution issue lifecycle convention (H4's audit issues).

---

## §8. Unsupported-provider backlog and operator controls

A research-only result must never disappear when the discovered provider has no adapter. Preserve the
originating issue and evidence, but record the actionable gap in the canonical machine-readable tracker
`config/discovery/pending-providers.yml`. A generated or maintained rolling issue may summarize counts
for maintainers, for example:

```md
## Unsupported agenda-provider backlog

| Provider | Cities pending | Status | Next action |
|---|---:|---|---|
| PrimeGov | 4 | adapter needed | build provider adapter |
| OneMeeting | 5 | adapter needed | build provider adapter |
| Agenda PE | 2 | research needed | verify platform scope |
```

Each tracker entry retains the city slug, originating issue, evidence URL, discovered/last-checked
timestamps, provider key, adapter status, and next action. A city assigned to this backlog is removed
from weekly auxiliary discovery noise until manually rechecked or the adapter becomes available.

Maintainer-only slash commands are documented in the originating issue body and must be idempotent:

```text
/r12 assign-provider <provider-key>
/r12 create-provider <key> name="..."
/r12 recheck
/r12 defer-agenda until=YYYY-MM-DD reason="..."
/r12 clear-disposition
```

`assign-provider` records a known unsupported Appendix-P provider. `create-provider` adds a new research
category only after a maintainer confirms it is genuinely distinct. `defer-agenda` sets
`known-no-agenda` plus `next_check_at`, which suppresses weekly checks until that date. None of these
commands deletes or hides the originating issue; they change its active queue state while preserving
the evidence and adding it to the visible backlog count.

## §9. Website, Discord, and Discussions intake

R12 should accept requests from people without GitHub or Discord accounts through a static website form.
The initial free path is **Formspark + a small Cloudflare Worker + Discord webhook + GitHub App + Resend**:

```text
website form
  → Formspark validates Turnstile and records the submission
  → Formspark webhook at an unguessable Worker URL
      → Cloudflare Worker
          → private deduplication/contact record
          → GitHub issue with add-city + source:website + needs:discovery
          → Discord notification containing the issue URL
          → requester acknowledgement email via Resend
```

According to [Formspark pricing](https://formspark.io/pricing/) and its [webhook documentation](https://documentation.formspark.io/integration/webhooks.html),
the current free account starts with 250 submissions and supports notification emails and webhooks. Its
webhook requests are not signed, so the Worker requires an unguessable secret URL segment, validates
the payload, and deduplicates submissions. Formspark validates the one-time Turnstile token before
sending the webhook; the Worker must not attempt to redeem that same token again. The Worker creates issues
directly through a GitHub App installation token; Discord is a notification surface, not the authority
for repository writes. Resend's free tier is sufficient for low-volume acknowledgements and has a
3,000-email/month, 100-email/day limit ([Resend pricing](https://resend.com/pricing)). If volume or automation needs grow, the form endpoint can remain
unchanged while the Worker gains GitHub issue webhooks for lifecycle emails. The private requester record
must never be placed in the public issue body.

Formspark also advertises a one-time submission bundle, but the initial design must not depend on that
purchase or assume it unlocks undocumented premium automation. The Worker owns the GitHub/Discord/email
glue regardless; the bundle is only an optional future capacity purchase if the free allowance becomes
insufficient.

The initial acknowledgement includes the public issue URL and explains how to follow status. Later
status emails are a follow-on: GitHub issue events map the issue number to the private contact record.
The `citymeetings.fyi` sending/reply identity is a maintainer-provided configuration input, not a
committed secret; implementation will document the required Formspark, Cloudflare, GitHub App, Resend,
DNS, SPF/DKIM/DMARC, Turnstile, and webhook settings as they are provisioned.

### §9.1 Requester email templates and future branding

R12 must prepare email templates as separate branded HTML and plain-text variants. The initial templates
should be content-complete but use design tokens/partials that the future R8 website design phase can
replace or align without changing the workflow logic. The sender/reply identity will be provisioned at
`citymeetings.fyi` by the maintainer; no credentials or mailbox secrets belong in the repository.

Required templates:

- `submission_received`: confirms the request, gives the public issue URL, explains that discovery runs
  daily/overnight, and states how to follow progress.
- `evidence_ready`: explains that research is ready for maintainer review and links to the evidence issue.
- `batched_for_review`: links to the batch PR and explains that merge is still required.
- `applied`: gives the merged PR/commit and published-site expectation.
- `needs_more_information`: asks for a missing city website, meeting URL, or contact clarification.
- `research_only`: explains that an unsupported provider was found and links to the provider-gap tracker.
- `evidence_expired`: explains the 90-day expiry and links to the fresh-discovery request.

Each template has:

```text
HTML version: branded, responsive, accessible, restrained, with plaintext-equivalent content
Plaintext version: complete message with no dependency on HTML rendering
Subject: stable, recognizable prefix such as "City Meeting Podcasts — ..."
From: citymeetings.fyi mailbox configured by the maintainer
Reply-To: monitored citymeetings.fyi mailbox or configured support address
Footer: public project URL, privacy/contact note, and opt-out language where applicable
```

The HTML version should not hard-code the final R8 palette or typography. Use named tokens for colors,
font stacks, spacing, links, and callouts so the future website design cycle can unify the email and site
brand. The plaintext version is authoritative for meaning and must remain useful when images, styles, or
email-client HTML are unavailable.

Discord and GitHub Discussions remain alternate front doors. A Discord slash-command/modal or a
Discussion marked as a city request creates the same canonical `add-city` issue, tagged with
`source:discord` or `source:discussion`; all discovery, approval, batching, and evidence remain in GitHub.

## §10. Apply mechanism — maintainer-reviewed batch PR

**Resolved 2026-07-14 (maintainer decision): all approved add-city proposals are bundled into a
maintainer-reviewed PR.** R12 never commits directly to `main`, regardless of whether a patch is
additive. This removes the parallel permission path and gives maintainers one review surface.

The daily batch phase collects issues labelled `r12:approved` whose evidence is less than 90 days old.
It creates or updates one PR with a descriptive, non-phase-number title such as:

```text
Add verified cities: Gainesville, FL; Waco, TX
```

The PR body lists every originating issue, includes each proposal diff and verification summary, and
asserts that no existing configuration is overwritten unless a maintainer explicitly approves that
class of change. New-city requests that would modify an existing file are excluded from the normal
batch and placed in a separate review queue.

The batch workflow must:

1. Fetch a fresh `main` checkout.
2. Revalidate proposal age, issue state, and source evidence.
3. Re-check that target city/feed paths are still absent or unchanged as expected.
4. Stage exact target paths only; never use `git add -A`.
5. Run the full additive/deletion/modification diff backstop.
6. Run config and provider tests for the batch.
7. Open or update one PR and comment its URL on every source issue.

Maintainer commands are the only approval path:

```text
/r12 approve
/r12 reject reason="..."
/r12 batch
/r12 recheck
```

`/r12 approve` adds `r12:approved`; it does not write files. `/r12 batch` is an optional immediate
dispatch of the same batch logic used by the daily schedule. The PR remains the only repository write
path and is merged through the normal maintainer review process.

Evidence expires after **90 days** for both new-city and auxiliary-source proposals. On expiry, preserve
the original evidence, add `r12:expired` and `needs:discovery`, remove approval eligibility, and allow a
new discovery run to post fresh evidence. Expiry must never close or erase an unresolved request.

<!-- Superseded 2026-07-14: the original direct-to-main design is retained below only as historical
context for the research that led to the final batch-PR decision. -->

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

<!-- End superseded direct-to-main design. -->

## §11. New dependency, and risks

### §11.1 Tavily (new dependency)

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

### §11.2 Risks

- **First automation in this codebase with `contents: write` on `main`.** Every prior automation
  (`audit_feeds.py`, `availability-digest.yml`, R2's champion-routing ticket) only ever writes GitHub
  issues — this is a materially higher-trust capability, worth stating plainly rather than downplaying.
  Mitigated by §10's fresh-checkout validation + redundant diff-stat backstop + `lock.yml`'s proven credential
  pattern, but the risk class itself (a bug in the gating logic could, in principle, commit something
  unintended) is new to this project and should be reviewed with that in mind before shipping.
- **Vendor free-tier risk** (§11.1) — bounded by the graceful-degradation posture above.
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
- **Per-platform verifier functions are real work, not a detail** (§11.3) — as many small
  `verify_{platform}(url) -> bool` functions as census entries this actually tries against.
- **New-city mode's provider hints (from the `add-city` form) must never be trusted uncritically** —
  treated as search-disambiguation hints only, verified the same way as anything else (§3.2), matching
  R11's own "maintainer recollection ≠ confirmed fact" discipline.

### §11.3 Near-term verifier/adapter targets (corrected from the L2 pass)

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

## Implementation slices (issue titles retained for execution tracking)

Titles should describe the work, not use the R12 phase number:

1. **Add the civic-platform discovery client** — `citypods/discovery/search.py`, Tavily client wrapper,
   `TAVILY_API_KEY`, bounded search budget, caching, and SSRF-safe candidate handling.
2. **Add structured civic-platform classification** — `Task` Literal,
   `TASK_VERSIONS`/`TASK_PROMPTS`, and the mode-aware `classify-civic-platforms` prompt per §3.2.
3. **Verify civic platforms through real provider adapters** — platform signatures plus end-to-end
   sample-episode resolution, scoped to §11.3's near-term list.
4. **Run daily city discovery and weekly auxiliary eligibility** — `.github/workflows/city-discovery.yml`,
   daily schedule/manual dispatch, coverage measurement, `agenda-covered`/`known-no-agenda` state,
   90-day expiry, and rolling digest reconciliation.
5. **Process website and community city requests** — Formspark webhook receiver, Cloudflare Worker,
   GitHub App issue creation, Discord notification, private requester record, Resend acknowledgement,
   deduplication, and Turnstile/abuse controls.
6. **Add maintainer-operated discovery dispositions** — slash commands for approval, rejection,
   unsupported-provider assignment, new provider category, no-agenda deferral, and recheck.
7. **Track unsupported civic providers and pending cities** — canonical
   `config/discovery/pending-providers.yml`, visible backlog summary, issue cross-links, counts, and
   adapter-status transitions.
8. **Batch approved city proposals into one review PR** — fresh checkout, proposal revalidation,
   additive/deletion/modification backstop, exact-path staging, provider/config tests, PR creation or
   update, and source-issue comments. No direct-to-`main` path.
9. **Prepare branded requester email templates** — HTML plus plaintext fallback for acknowledgement,
   evidence, batching, applied, research-only, missing-information, and expiry states; tokenized so R8's
   future visual design can align email and website branding.
10. Optional, non-blocking: extend `add-city.yml`'s provider dropdown with Appendix P's fuller platform
    list. `Not sure`/`other` remain valid fallbacks.
