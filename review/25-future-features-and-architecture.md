# Future features, comparisons & architecture evolution (2026-07)

**Status: point-in-time proposal / idea-generation** · authored against `main` @ `2394d85` by an
independent reviewer (Claude Fable 5). This is the **forward-looking, expansion-oriented** companion to
the backward-looking code review in [`review/24`](24-comprehensive-code-review-2026-07.md).

It exists to answer three commissioned questions:

1. How does this project compare to similar projects and design paradigms?
2. What adjacent features would make it more useful to more people?
3. What architecture would those features require?

**Relationship to the canonical roadmap.** [`review/11`](11-technical-design-roadmap.md) is the living
design index and is deliberately exhaustive; VISION/ROADMAP own committed sequence. This document does
**not** reorder that plan or promote anything — per AGENTS.md, a real reprioritization is a maintainer
decision behind the deviation gate. Its job is to (a) situate the project against the field, (b) surface
feature ideas — including ones *not* currently in the catalog — with their user value and infra
adjacency, and (c) name the **one architectural seam the project is missing** that most of the
post-1.0 vision depends on. Ideas already in review/11 are cross-referenced, not re-proposed; genuinely
new ones are marked **NEW** and would enter the pipeline at L0/L1 (VISION or §6 deferred backlog) if the
maintainer adopts them.

---

## Part 1 — Comparison to similar projects & design paradigms

### 1.1 The civic-tech landscape this sits in

| Project / product | What it does | How this project differs / what it could borrow |
|---|---|---|
| **City Bureau *Documenters*** | Pays/train community members to attend & take notes at public meetings; human-first. | Documenters is human-labor-scaled and note-centric; this is automation-scaled and recording/transcript-centric. **Borrow:** their taxonomy of meeting types and their "assignment" model is a ready-made design for a future *crowdsourced correction* layer (§3.7). The two are complementary, not competing — a Documenters chapter is an ideal early adopter. |
| **City Bureau *CityScrapers*** | Open-source scrapers that normalize **upcoming** meeting schedules across agencies. | This project scrapes *recordings*; CityScrapers scrapes *calendars*. Phase F (upcoming-agenda scraping) is re-deriving part of CityScrapers. **Borrow:** their scraper-per-agency spider pattern and their normalized `Meeting` schema are prior art for the Phase-F provider `upcoming` capability — worth reading before designing it. |
| **DataMade *Councilmatic* (Chicago/NYC/etc.)** | Legislation-tracking sites over Legistar: bills, sponsors, votes, committees, search. | Councilmatic is *legislation*-centric (structured agenda items & votes); this is *recording*-centric (audio/transcript). They converge exactly at the Legistar provider (#31) + vote extraction (#8). **Borrow:** Councilmatic proves the value of the entity model (person ↔ bill ↔ committee ↔ vote) this project doesn't yet have (§3.5). |
| **Digital Democracy (CalMatters, ex-Cal Poly)** | CA legislature: transcription + **speaker identification** + searchable clip database + alerts. | This is the closest analogue to the *end-state* vision, but for one state legislature with hearing-room infrastructure. This project's differentiator is **breadth across small municipalities** with no such infrastructure. **Borrow:** their speaker-identification + per-legislator profile pages are the proven shape for diarization (#7) + a speaker-directory feature (§3.5). |
| **Legistar / Granicus / CivicClerk (the incumbents)** | The proprietary portals the recordings *live in*. | These are the "buried in proprietary video portals" problem VISION names. The project's whole thesis is to be the open, subscribable, searchable layer *over* them. Strategic note: staying a good citizen of their endpoints (the rate-limiting/lease work) is an ongoing cost of this positioning. |
| **NotebookLM / podcast-generation tools** | LLM turns documents into a two-host audio discussion. | Phase C (AI-generated discussion podcast with real clips) is this, plus the project's unique asset — **real meeting audio via the EDL/clip machinery**. That clip-grounding is the differentiator NotebookLM can't replicate (it has no source audio). |
| **Generic "meeting summarizer" SaaS (Otter, Fireflies, etc.)** | Transcribe + summarize corporate meetings. | Same ASR/LLM tech, entirely different trust model: those *overwrite* with AI freely; this project's **"never editorialize the factual record, LLM output is untrusted"** rule is a deliberate civic-integrity stance those products don't need. Worth keeping as a marketed *feature*, not just an internal invariant. |

**Takeaway:** the project occupies a real and mostly-empty niche — *automated, breadth-first,
subscribable and searchable coverage of small-municipality meeting recordings, with a civic-integrity
guarantee.* The
adjacent projects validate the individual downstream features (entity model, speaker ID, calendar
scraping, AI audio) and in several cases offer reusable prior-art schemas.

### 1.2 Design-paradigm placement (and what it implies)

The system is, in established software terms, a clean composition of four paradigms:

- **Event sourcing + CQRS-lite.** `records.merge_persisted` is an append-only event log; feeds, city
  pages, `/admin/status`, and (future) search indexes are *projections* rebuilt from it. This is why
  "never lose a dropped meeting" is structurally guaranteed rather than hoped for.
- **Content-addressable storage.** Audio/transcript keys embed a spec hash (Git/IPFS lineage). Gives
  cache-busting, rollback, dedup, and orphan-GC for free.
- **Jamstack / static-first delivery.** Pre-rendered `docs/` on Pages + CDN; $0 egress; no server to
  attack or scale for reads.
- **Pluggable-backend (hexagonal / ports-and-adapters).** `storage/`, `compute/`, and `providers/` are
  all Protocol + registry seams. New platform = adapter; new per-episode feature = stage.

Two paradigms the project has **partially reinvented** and could name explicitly:

- **Durable execution / distributed leases** (Temporal / DBOS / Cloudflare Queues shape) — the
  work-lease + budget + provider-lease plane (see review/24 §S3). Already built; worth recognizing as
  a first-class subsystem with its own invariants and test strategy.
- **The missing one: a dynamic edge tier.** Everything above is *read-only-at-serve-time*. There is no
  paradigm in place for *per-user state, writes at request time, or query-time computation* — which is
  what alerts, watchlists, personalization, custom-query feeds, and an API all need. The project has the
  raw ingredients (a Cloudflare Worker exists; `RoutingStorage` exists; review/17 plans records→SQL) but
  no **seam** for "a small, stateful, request-time compute tier that never compromises the static public
  record." **This is the central architectural recommendation of Part 3.**

---

## Part 2 — Feature proposals

Grouped by the user each serves, with **value**, **infra adjacency** (what already exists that it
leans on), and **catalog status** (whether it's already in review/11, and where it would slot). Effort
uses review/01's legend (S ≤½d · M 1–2d · L ~week · XL multi-week). Items marked **NEW** are not in the
current catalog.

### 2.1 For the engaged resident (subscribe, search, share)

1. **Per-meeting permalink pages with rich share cards** — *Value: high · Effort: M.* Already R1
   ([`review/13`](13-per-meeting-pages-and-search.md)); the **NEW** addition worth folding in is
   first-class **SEO/share infrastructure**: `schema.org` `Event`/`VideoObject`/`Transcript` JSON-LD,
   OpenGraph/Twitter cards, and an oEmbed endpoint so a meeting pastes richly into social/blogs.
   *Adjacency:* pure render-stage + template work; the EDL/transcript/chapters data already exists.
   This is the single biggest discoverability multiplier — a static page per meeting is what makes the
   archive crawlable and quotable, and the share metadata is what makes a shared clip spread.
2. **Semantic transcript search (beyond keyword)** — *Value: high · Effort: L · NEW (extends R2).*
   R2 is client-side keyword search (Pagefind). The **NEW** layer is embedding-based semantic search:
   "find where they talked about *traffic calming*" matches "chicanes," "speed humps," "road diet."
   *Adjacency:* the word-JSON sidecar (H12) is the ideal chunking unit; embeddings are a new **LLM/GPU
   backend `embed` verb** on the already-designed compute seam. *Architecture:* two tiers — a
   pre-computed static nearest-neighbor index for small catalogs (still Jamstack), graduating to a
   Cloudflare **Vectorize** index behind a Worker at scale (Part 3). This is where keyword search hits
   its ceiling for civic research.
3. **"Jump to agenda item" deep navigation** — *Value: high · Effort: M · partially R4.* Bind chapter/
   agenda-item boundaries to transcript spans so a page has a clickable agenda that seeks the player and
   scrolls the transcript. *Adjacency:* chapters + timeline EDL + word-JSON already give every input;
   this is a consumer, not new capture. The extractive "what changed" card (#3) is the sibling.
4. **Spanish (and multilingual) transcripts/summaries** — *Value: high in TX · Effort: M · deferred #9.*
   Worth **promoting from deferred** given the pilot geography. *Adjacency:* another `translate` verb on
   the LLM compute seam; alternate `<podcast:transcript>` with an `hreflang`, alternate summary blocks.
   Cost-gated and clearly-labeled per the untrusted-output rule. For a Texas audience this is arguably a
   1.0-relevant accessibility feature, not a post-1.0 nicety.
5. **Embeddable player/clip widget** — *Value: med · Effort: M · NEW.* An `<iframe>`/web-component that
   embeds a single meeting or a specific clip (via the EDL clip service) on a neighborhood blog or a
   Strong Towns chapter site. *Adjacency:* `clips.extract_clip` already exists but is **unwired** (no
   consumer today — confirmed); a widget + a soundbite feed are its first two consumers. This turns every
   local group's existing website into a distribution channel.

### 2.2 For the activist / organizer (act before the vote)

6. **Watchlists + topic alerts (RSS-first, then email/push)** — *Value: very high · Effort: L · Phase F.*
   The single feature that converts the project from *archive* to *organizing tool*: "tell me when
   parking minimums hit an agenda." *Adjacency:* topic tags (#4/R3) + Phase-F upcoming-agenda scraping
   supply the matching data. *Architecture:* the **first feature that needs the dynamic tier and the
   first PII** — RSS-per-topic is static and PII-free (do that first), but email/push needs a subscriber
   store (Part 3). This is the highest-leverage item in the whole vision and the one most blocked by the
   missing seam.
7. **Upcoming-agenda + staff-report ingestion** — *Value: very high · Effort: L · Phase F.* Already
   sketched (review/11 §5.3). Prior art: **CityScrapers** (§1.1) — read it before designing the provider
   `upcoming` capability. *Adjacency:* extends the provider Protocol with one capability; Legistar/
   CivicClerk expose structured events, Granicus/Swagit need agenda-portal scraping.
8. **Vote/roll-call + attendee extraction → "how did my councilmember vote"** — *Value: high (civic) ·
   Effort: M · Phase F (#8/#14).* From **platform metadata + released minutes, never inferred from audio**
   (the existing rescope is correct). *Adjacency:* a shared "minutes-ingestion" component + the entity
   model (§3.5). This is a Councilmatic-class feature and a journalist magnet.
9. **Agenda-packet (PDF) analysis** — *Value: high · Effort: L · Phase F.* The real decision detail is in
   staff-report PDFs. Fetch → extract text → structured/LLM "what's being proposed" brief, additive and
   labeled. *Adjacency:* a new `documents` provider capability + PDF text extraction + the LLM
   `summarize` verb. Pairs with #7 and feeds the alert matcher.

### 2.3 For the journalist / researcher (the raw material)

10. **Public data API + bulk export** — *Value: high · Effort: L · NEW (enabled by review/17).* Machine-
    readable access: per-meeting JSON, transcript/word-JSON, votes, tags; bulk `.jsonl`/`.csv` dumps;
    an OpenAPI-described query endpoint. *Adjacency:* the review/17 records→SQL item is the enabler;
    a read-only Worker over that DB is the surface. This is what makes the project *infrastructure* for
    other civic-tech rather than an endpoint — a small feature with outsized ecosystem value.
11. **Speaker diarization + speaker-directory pages** — *Value: med-high · Effort: L · Phase R (#7).*
    Diarization is already designed (review/11 §5.1). The **NEW** extension is the *product* on top:
    per-speaker pages ("everything Councilmember X has said, across meetings"), which is Digital
    Democracy's proven high-value surface. *Adjacency:* diarization writes `speakers.json`; the entity
    model (§3.5) links speaker → person → votes. Deliberately keep it *identify-then-human-confirm*, not
    auto-name, given the integrity rule.
12. **Cross-meeting knowledge graph** — *Value: med · Effort: XL · NEW.* Link ordinances, addresses,
    dollar amounts, projects, and people *across* meetings and cities ("every mention of the Comprehensive
    Plan update"). *Adjacency:* transcript + minutes + votes are the raw nodes; this is the long-horizon
    payoff of the entity model. Note: this is the ambition #5 (NER) was *deleted* for — worth revisiting
    only once the structured entity data (§3.5) exists as better ground truth than raw NER.

### 2.4 For reach & sustainability (grow the audience, fund the work)

13. **"National highlights" reel + weekly look-back digest + site-news RSS** — Phase E, already sketched.
    The **NEW** amplifier: **social syndication** — an automated Bluesky/Mastodon/ActivityPub bot that
    posts a clip + quote + source link per highlight. *Adjacency:* soundbites (#15) + the clip service +
    the deep-link capability. Near-zero-cost reach that meets the "expose the raw material" stance.
14. **Custom-query feed builder + OPML** — Phase E (#12/#13/#17). *Architecture:* pre-generated combos are
    static (do first); a truly custom builder needs the dynamic tier (Part 3). OPML export is trivially
    static and a do-now.
15. **`<podcast:funding>` + privacy-respecting analytics (OP3)** — Phase R/E (#16/#125). Near-$0, funds
    everything else, unblocks the "is this feed worth investing in" signal. Genuinely do-now.
16. **AI-generated discussion podcast with real clips** — Phase C, the furthest-horizon differentiator.
    Its uniqueness (real meeting audio via EDL, not just TTS over text) is exactly what §1.1 shows the
    NotebookLM-class tools *cannot* do. Revenue-gated, correctly deferred.

### 2.5 For coverage & operations (more cities, less toil)

17. **More providers: Legistar (InSite API), YouTube gov channels, Vimeo, Zoom, BoxCast, Cablecast** —
    Phase F+ (#31). Legistar is the highest-value (structured agendas/votes/rosters). *Adjacency:* each is
    one adapter against the frozen provider Protocol — the paradigm is proven, this is throughput.
    YouTube gov channels are notable as the widest-coverage untapped source for the smallest cities.
18. **Auto-detect provider from a city URL + city-request `/approve` onboarding** — #30/#28. Lowers the
    marginal cost of adding a city toward zero, which is the precondition for the breadth phase.
19. **Public data-quality / provenance dashboard** — *Value: med · Effort: S · NEW.* A *public* (not just
    `/admin`) page showing per-city coverage, transcript status, and known gaps, with clear provenance
    ("this recording was withheld as empty"). *Adjacency:* `/admin/status` + `media_availability` already
    compute all of it; this is a trust feature — showing the seams openly is itself the civic-integrity
    stance made visible.

---

## Part 3 — Architecture evolution

Most Part-2 items land as **stages, adapters, or new compute-backend verbs** and need *no* structural
change — that is the payoff of the existing seams, and it should be stated plainly so the project
resists over-building. The proposals below are the exceptions: the handful of features that genuinely
exceed what the current architecture can express, and the seams they imply.

### 3.1 The central recommendation: a dynamic edge tier (the "Interaction" seam)

**Problem.** Alerts/watchlists (#6), email/push, personalization, a custom-query feed builder (#14), an
API (#10), and semantic search at scale (#2) all require *state written at request time* and/or
*computation at query time*. The static-first architecture structurally cannot do this, and today there
is no seam for it — unlike storage and compute, which have clean Protocol+registry seams.

**Proposal.** Introduce one new port — call it the **Interaction backend** — mirroring the existing
`storage`/`compute` seam pattern, with `local`/`none` as the default (so 1.0 and dev are unaffected) and
**Cloudflare Workers** as the first real adapter. It already fits the stack: the granicus-media-proxy
Worker proves the deployment path, R2 is already the coordination backend, and the ecosystem parts map
cleanly:

- **D1** (SQLite at the edge) — the review/17 records→SQL target; backs the API and query features.
- **Vectorize** — the semantic-search index (§2.1 #2).
- **Queues + Durable Objects** — the alert fan-out and the subscriber/watchlist state (§2.2 #6).
- **KV** — hot config / feature flags.

**Invariant to hold (non-negotiable, from VISION/SECURITY):** the public record stays static, free, and
un-paywalled. The dynamic tier is strictly *additive* — it reads the same durable artifacts and adds
personalization/query/alerting *around* them; it must never become a required path to read a meeting.
Concretely: Pages keeps serving the archive even if the Worker tier is down; the Worker tier is a
progressive enhancement. This is the same "degrade to static" discipline the storage router already
uses ("coordination absent → fall through to primary").

**Why now (as a design, not a build):** this is a **pre-scale lock** in exactly the spirit of the
existing "compute is pluggable" pre-1.0 lock. Deciding the Interaction seam's shape *before* the first
alert/API feature is built means every later interactive feature is an adapter/handler, never a
re-architecture — the same bet the project already made (correctly) on storage and compute.

### 3.2 First PII & identity — design the trust boundary before the first subscriber

Email/push alerts introduce the project's first personal data. The SSRF/untrusted-input boundary is
already firm; a **subscriber-data boundary** is greenfield. Design it *with* the Interaction seam:
double-opt-in, RSS/webhook-first (PII-free) as the default channel, minimal retention, no per-user
tracking (consistent with the OP3 aggregate-analytics choice), and a clear separation between the
public static record and the private subscriber store. Getting this boundary explicit up front is the
same "trust boundary is firm before onboarding opens" move `security.py` made for sources.

### 3.3 Search that outgrows the client

Pagefind/client-side search is right for hundreds of feeds (review/16's budget) and should ship as R2.
The graduation path — stated so it isn't a surprise — is: (1) partitioned client-side index by
city/source (already in review/13); (2) a Worker-backed full-text index (D1 FTS5) when the client
budget is crossed; (3) the Vectorize semantic layer (§2.1 #2) as a parallel index, not a replacement.
All three are the Interaction seam; keyword and semantic are two backends behind one search handler.

### 3.4 The entity model — the missing data spine

Votes (#8), attendees (#14), speaker directories (§2.3 #11), the knowledge graph (#12), and Councilmatic-
class navigation all need a **normalized entity layer** the record model doesn't have today: `Person`,
`Body`, `AgendaItem`, `Vote`, `Document`, with stable IDs and cross-meeting linkage. *Adjacency:* this
is the natural schema of the review/17 SQL store, and the append-only record is the event log it
projects from. **Recommendation:** when records→SQL is designed (Phase R, trigger-gated), design the
entity tables *at the same time* even if only meetings/episodes populate them at first — retrofitting an
entity spine after the API ships is far more expensive than reserving it. This is the data-model analogue
of the compute-seam pre-lock.

### 3.5 Compute-seam extensions (already-designed seam, new verbs)

The compute backend's task verbs (`transcribe`/`align`/`diarize` + reserved `summarize`/`tag`/
`soundbite-select`) cover most LLM/GPU needs. The Part-2 features add three **NEW verbs** that fit the
same `InferenceJob(task, inputs, recipe_hash)` contract with no seam change: **`embed`** (semantic
search / clustering), **`translate`** (multilingual), and **`extract`** (structured vote/entity/agenda
extraction from minutes/packets, LLM-assisted with human confirm). Folding `prompt_hash` + `model_id`
into the recipe hash (already the plan for LLM verbs) gives version-aware re-derivation for all three.
Keep the untrusted-output rule on every one.

### 3.6 Provider Protocol extension for foresight

Phase F's `upcoming`/agenda/`documents` capabilities are the one provider-Protocol *extension* (not just
new adapters) on the horizon. Design it as additive capability tokens (like the existing `deeplink`
capability) so providers that can't do foresight simply don't declare it — and read CityScrapers'
normalized schema first (§1.1) rather than inventing one.

### 3.7 A crowdsourced-correction layer (community integrity, longer horizon) — NEW

ASR is imperfect; the civic-integrity stance is a differentiator; Documenters proves communities will do
this labor. A **human-correction layer** — suggest an edit to a transcript cue or a speaker label,
maintainer/community-moderated, stored as an *overlay* on the content-addressed artifact (never
overwriting it) — would improve quality *and* deepen engagement. *Adjacency:* the append-only +
content-addressed model is exactly right for an overlay (the correction is a new event, the original is
immutable); the Interaction seam supplies the submission path. This is speculative and post-1.0, but it
aligns unusually well with both the architecture and the mission, so it's worth capturing as an L0 idea.

---

## Part 4 — Synthesis: what to consider adding to the roadmap

Nothing here overrides the committed sequence; these are the items an independent read would flag as
**highest-leverage or currently-unrepresented**, for the maintainer to weigh:

1. **Adopt the Interaction-seam design (§3.1) as a pre-1.0 or early-post-1.0 lock**, exactly as
   "compute is pluggable" was. It unblocks the largest share of VISION (alerts, API, personalization,
   scaled search) and, undesigned, it is the thing that will force a re-architecture later.
2. **Reserve the entity-model schema (§3.4) when records→SQL is designed**, even before it's populated.
3. **Consider promoting Spanish/multilingual (§2.1 #4) from deferred**, given the TX pilot geography —
   it reads as a 1.0-relevant accessibility feature there, not a post-1.0 nicety.
4. **Wire the already-built clip service** (§2.1 #5): `clips.extract_clip` exists with no consumer; a
   soundbite feed + an embeddable widget are two cheap, high-reach first consumers.
5. **Treat SEO/share metadata as part of R1 pages** (§2.1 #1), not a later polish — it's what makes the
   archive discoverable and shareable, and it's cheap render-stage work.
6. **New L0/L1 ideas to capture** (not currently in the catalog): public data API (#10), semantic search
   (#2), social syndication bot (#13), public data-quality dashboard (#19), knowledge graph (#12),
   crowdsourced correction (§3.7).

## Cross-references

- Companion code review: [`review/24`](24-comprehensive-code-review-2026-07.md).
- Canonical forward design & catalog: [`review/11`](11-technical-design-roadmap.md); vision & near-term:
  [VISION.md](../VISION.md), [ROADMAP.md](../ROADMAP.md).
- Enablers referenced: pages+search [`review/13`](13-per-meeting-pages-and-search.md), tags
  [`review/14`](14-topic-tags-strong-towns-lens.md), Legistar [`review/15`](15-legistar-catalog-provider.md),
  scaling [`review/16`](16-scaling-review-plan.md), records→SQL [`review/17`](17-state-store-backend-evaluation.md),
  work distribution [`review/18`](18-work-distribution-sharding.md).
