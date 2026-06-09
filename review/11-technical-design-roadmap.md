# Technical Design Roadmap (canonical, living)

**Status: LIVING · last updated 2026-06-08**

This is the canonical **forward design** reference for the project — the single map of every initiative
needed to deliver [ROADMAP.md](../ROADMAP.md) and [VISION.md](../VISION.md), the maturity of each, and a
pointer to its detailed design. It exists to:

- be the place a developer or AI agent goes to **pick the next thing to build** with full understanding;
- **seed GitHub Issues** (a development-ready entry contains enough to file issues with no further design);
- keep the project's design documentation from **going stale** (the failure mode of `review/00–10`).

Unlike `review/00–10` (point-in-time snapshots), **this document is kept current**. Its companions:
[ARCHITECTURE.md](../ARCHITECTURE.md) (as-built), [CHANGELOG.md](../CHANGELOG.md) (what shipped),
[CONTRIBUTING.md](../CONTRIBUTING.md) (process — the *normative* copy of the lifecycle below).

This roadmap also folds in and supersedes the standing parts of the **Codex review**
([`review/10`](10-codex-architecture-throughput-roadmap-review.md)); see §8 for the appraisal and the
post-review code queue.

---

## §1. How to use this document

1. Find an initiative in the **current phase** (Phase H first) whose **maturity is L3**.
2. Open its **breakout doc** (`review/12+`) and implement from the file/test/sequencing plan there.
3. Update the docs per the **lifecycle contract (§2)** in the same change — including flipping this
   document's catalog entry and adding a [CHANGELOG.md](../CHANGELOG.md) line.
4. When a planned development step is completed (especially once its PR merges), do the **Implemented**
   row in §2 immediately: mark it Shipped here with the PR link, add the CHANGELOG entry, stamp/freeze
   its breakout, move/narrow the ROADMAP item, and update ARCHITECTURE if the shipped code changed the
   as-built system shape.
5. Before committing an edit to *this* file, run the **standing verification checklist (§7)**.

---

## §2. Feature lifecycle & doc-update contract

The normative copy lives in [CONTRIBUTING.md](../CONTRIBUTING.md); this is the mirror. Every feature
travels this pipeline; when you move it forward, update the listed docs **in the same change**:

| Stage | Trigger | Update (change X → update Y) |
|---|---|---|
| **Idea (L0)** | captured | `VISION.md` (long-horizon) **or** this doc's Deferred-backlog (§6) |
| **Committed (L0→L1)** | promoted to near-term | `ROADMAP.md` + catalog (§4) at L1 + write the L1 sketch inline (§5) |
| **Designed (L1→L2)** | approach chosen / next-up | **break out** to a new `review/NN`; catalog entry → L2 + link |
| **Dev-ready (L2→L3)** | full design done | mature `review/NN` to L3; **cut GitHub Issue(s)**; catalog → L3 |
| **Implemented** | PR merged | catalog → **Shipped** (+PR link); add **CHANGELOG.md** entry; update **ARCHITECTURE.md** if architecture changed; **freeze + stamp** the breakout ("Implemented in PR #N"); move ROADMAP item to "Recently shipped"; close/narrow issues; capture any durable decision in the relevant committed doc |
| **Superseded** | abandoned/replaced | mark the catalog entry; note in CHANGELOG if ever partially shipped |

---

## §3. Maturity ladder (and where each level lives)

| Level | Meaning | Lives in | Promote when |
|---|---|---|---|
| **L0 Idea** | named only | `VISION.md` / `ROADMAP.md` (+ catalog row) | an approach is worth sketching |
| **L1 Sketch** | problem + 1–3 candidate approaches, rough tradeoffs | **inline in this doc (§5)** | an approach is chosen / it's next-up |
| **L2 Designed** | chosen approach; data-model deltas; module/file plan; interfaces; risks | **breakout `review/NN`** | the design is detailed enough to estimate |
| **L3 Dev-ready** | full 08/09 depth: concrete file/function changes, test plan, sequencing/DAG, migration/backfill, acceptance criteria | the **same breakout**, matured | ready to cut into Issues & build |

**Break out at L1→L2.** A breakout doc is born when an initiative is committed to an approach and is
about to be worked next (or when its design exceeds ~one screen). Until then it stays an L1 sketch here.

---

## §4. Initiative catalog (exhaustive)

Every feature named anywhere in the project's docs (review/01 #1–#57, review/02–09, ROADMAP, PLAN, and
open issues) appears here, in a phase or the Deferred backlog (§6). `#NN` = review/01 roadmap item;
`GH#` = GitHub issue.

### Shipped (foundations) — see [CHANGELOG.md](../CHANGELOG.md)
INFRA-1..9 timeline foundation (GH#141) · #52 content permanence (absorbs **#45** self-healing
dead-enclosure re-resolve) · #51 official meetings link · #22
silence-trim · #21 loudness · #23 host-all · #122 concat · clips service · #1/#110 ASR · #11
`<podcast:transcript>` · #124 status dashboard/admin (#49 partial) · #29 SSRF gate · #35 projection ·
#36 run-history · #37 endpoint contracts · #38 retry/backoff · #43 alias validation · review/02 Change 8
provider capabilities · resource/agenda links + `content:encoded` + `<podcast:chapters>` · durable state
sync · #20 video enclosures (partial).

### Phase H — Hardening & Efficiency · breakout [`review/12`](12-hardening-and-efficiency.md)

> **Reprioritized 2026-06-08** (build-log root-cause analysis — see
> [review/12 § Build-log root-cause analysis](12-hardening-and-efficiency.md#build-log-root-cause-analysis-2026-06-08)):
> **H10 shipped in PR #232** and **H8 shipped in PR #235**; the remaining do-now reliability item
> **H11a** runs **ahead of H1–H5**. These fixes address what is actually turning Build & Deploy red on
> ~half of scheduled runs — runner
> starvation (unpinned ffmpeg `-threads` + no memory admission control; `continue-on-error` can't catch a
> runner-level SIGTERM/lost-comms). H10 fixed the broken ASR `align` path that yielded zero transcripts
> for caption-bearing feeds.

| Item | #/GH | Maturity | Status |
|---|---|---|---|
| H1 docs/issue reconciliation | #52-health, GH#110/#141/#154 | L3 | in progress (this doc set) |
| H2 projection wall-clock fix | R3 | L3 | committed |
| H3 feed-validation publish gate | #53 | L3 | committed |
| H4 feed-health catch-up vs stalled states | R5 | L3 | committed |
| H5 stage backlog manifest + prioritization policy | #41, R2 | L3 | committed · include `transcript-align` backlog lane |
| H6 ASR benchmark workflow → sharded ASR workflow | #1, R1 | L3 | committed · split align-only vs transcribe-only ASR lanes |
| H7 contributor/agent handoff docs | #57 (partial), R9 | L3 | **Shipped** (this doc set: AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING + templates) |
| H8 4-core runner saturation (ffmpeg `-threads` + memory admission + abandoned-thread accounting) | new | L3 | **Shipped** ([PR #235](https://github.com/BashfulBits/city-meeting-podcasts/pull/235)) |
| H9 free transcription-offload evaluation | new | L2→L3 | committed |
| H10 ASR alignment fix (`WhisperModel.align` AttributeError + fallback gap) | new | L3 | **Shipped** ([PR #232](https://github.com/BashfulBits/city-meeting-podcasts/pull/232)) |
| H11 deploy resilience (survive runner-level kill; later isolate enrich) | new | L3 | committed · **do-now** native audio/ASR gate + one-slot audio lane (H11a), then measured 3-core ASR / 1-core audio tuning only after green runs |
| #39 per-provider rate limiting | #39 | L2 | committed (efficiency-adjacent) |

### Phase R — Research-Tool Surface (toward 1.0)
| Item | #/GH | Maturity | Breakout |
|---|---|---|---|
| Per-meeting permalink pages | #46/GH#157 | L2→L3 | [`review/13`](13-per-meeting-pages-and-search.md) |
| Static transcript search | #6 | L2→L3 | [`review/13`](13-per-meeting-pages-and-search.md) |
| Topic tags / Strong Towns lens | #4 | L2→L3 | [`review/14`](14-topic-tags-strong-towns-lens.md) |
| Per-agenda-item "what changed" cards | #3/GH#155 | L1 | §5.1 |
| Auto-summaries | #2 | L1 | §5.1 |
| Soundbite highlights | #15/GH#156 | L1 | §5.1 |
| Front-end design cycle | #55 (#20/#54) | L1 | §5.1 |
| Accessibility (WCAG) | #50 | L1 | §5.1 |
| `<podcast:funding>` link | #16 | L1 | §5.1 |

### Phase E — Engagement & Distribution (post-1.0) · sketches §5.2
| Item | #/GH | Maturity |
|---|---|---|
| Site-news RSS + static digest pages | new (Feature A) | L1 |
| Weekly look-back digest | new (Feature A) | L1 |
| "National highlights" curated reel | new (Feature A) | L1 |
| Substack newsletter channel | #18 (email split) | L1 |
| Topic/issue + region roll-up feeds | #12/#13 | L1 |
| Custom-query feed builder | #12+#13 | L1 |
| OPML export | #17 | L1 |
| Privacy-respecting download analytics | GH#125 | L1 |

### Phase F — Pre-Meeting Foresight (post-1.0) · sketches §5.3
| Item | #/GH | Maturity |
|---|---|---|
| Upcoming-agenda + staff-report scraping | new (Feature B) | L1 |
| Upcoming-meetings `.ics` calendar | #19 | L1 |
| Watchlists + topic alerts | new (R8 extension) | L1 |
| Backup-material (packet) analysis | new (Feature B) | L1 |
| Legistar provider (rich agendas/votes/rosters) | #31 | L1 |
| Vote/roll-call extraction (metadata + minutes) | #8 | L1 |
| Attendee extraction (from minutes) | #14 | L1 |

### Phase C — Co-Creation & AI Audio (furthest horizon) · sketches §5.4
| Item | #/GH | Maturity |
|---|---|---|
| Look-ahead / look-back outlines & drafts | new (Feature C) | L1 |
| AI-generated discussion podcast w/ meeting audio | new (Feature C) | L1 |

### Cross-cutting / ongoing · sketches §5.5
| Item | #/GH | Maturity |
|---|---|---|
| Strong Towns-focused city discovery | #27/#32 (rescoped) | L1 |
| City-request issue + `/approve` onboarding | #28 | L1 |
| User "report a feed problem" template | #56 | L1 |
| Auto-detect provider from a city URL | #30 | L1 |
| Contributor scaffolding (labels, PR template, board) | #57 | L1 (partial: handoff docs shipped) |

### Deferred backlog (ongoing) — §6
#7 diarization · #9 translation · #24 bitrate ladders · #25 intro/outro stinger (GH#153) · #26 chapter
images · #34 config-via-issue-comments · #40 B2 actual-cost dashboard · #42 index sharding (review/02
Change 6) · #44 structured logging · #47 map browser · #48 new-since-visit · #10 agenda-packet chapter
descriptions · #14 `podcast:person/location` tags · #33 dead-city archival · review/02 Change 5
DerivedArtifact refactor · review/04 B3 stale-record bucket leak · review/04 R4 per-host rate-limit (#39)
· admin dashboard extension (#49) · full video re-hosting · hosted DB/API · off-Actions media.
**Deleted:** #5 entities/NER.

---

## §5. L1 sketches (initiatives not yet broken out)

Each: problem + 1–3 candidate approaches + rough tradeoffs. Promote to a breakout (L2) when chosen.

### §5.1 Phase R remainder

**Per-agenda-item "what changed" cards (#3/GH#155).** *Problem:* a freeform meeting summary is risky and
low-trust; residents want "what did they decide on item 7?" *Approaches:* (1) **extractive** — join
chapter/agenda-item boundaries (already have chapters) with the transcript span + official
minutes/vote metadata into a structured card (title, action, vote, excerpt+timestamp, doc link), no LLM;
(2) **LLM-assisted** — same skeleton, LLM drafts a one-line "what changed" per item from the transcript
span, clearly labeled + cached. *Tradeoff:* (1) is auditable and ~$0 but only as rich as the source
metadata; (2) adds cost + the untrusted-output rule. **Lean: (1) first, (2) additive.** Depends on tags
(#4) for topic chips. Auditable structure precedes any freeform summary (#2).

**Auto-summaries (#2).** *Problem:* a short "what happened" blurb in notes/pages. *Approaches:* (1)
agenda-derived templated blurb ($0); (2) LLM 3–5 sentence summary from transcript (sub-cent–3¢/meeting,
cached). *Tradeoff:* cost-gated (<$20/mo near-term); never overwrites official text. Build **after** #3
cards so summaries cite structured items.

**Soundbite highlights (#15/GH#156).** *Problem:* surface a shareable ~60s clip per meeting. *Approach:*
consume the existing EDL via `clips.extract_clip` (forward-map a served range → source cuts); selection
is (1) manual/config, (2) heuristic (longest public-comment turn / agenda-item peak), or (3) LLM-picked
from the transcript. *Tradeoff:* infra exists; only selection is new. Powers `<podcast:soundbite>` +
the highlights reel (Phase E).

**Front-end design cycle (#55, absorbs #20/#54).** *Problem:* index accordion polish, subscribe-button
**app iconography**, clear **audio-vs-video** labeling. *Approach:* iterative mockup-driven redesign;
coordinate with per-meeting pages (R1). 1.0-gating. *Tradeoff:* design effort, low risk.

**Accessibility (#50).** WCAG pass on generated pages (player labels, contrast, keyboard nav, transcript
semantics). 1.0-gating. Pairs with R1 pages.

**`<podcast:funding>` link (#16).** Trivial feed tag + config; funds the rest. Near-$0 do-now.

### §5.2 Phase E — Engagement & Distribution

**Site-news RSS + static digest pages.** *Problem:* let people subscribe to the *project*, not just one
city. *Approach:* a generated `docs/news/` with static digest pages + a dedicated `news.xml` RSS;
content is auto-generated outlines (feature announcements + cross-city highlights). *Tradeoff:* RSS-first
= no PII/infra (fits the static model). Foundation for the newsletter.

**Weekly look-back digest.** *Problem:* "what happened in your city's meetings this week." *Approach:*
aggregate recent records (titles, #3 cards, tags, soundbites) per city/region into a static page + RSS
item; optionally per-topic. *Tradeoff:* extends #3/#4; the bridge to Feature C look-back outlines.

**"National highlights" curated reel.** *Problem:* make land-use/fiscal processes legible to a wide
audience (good public comment, good council members, transparently-quoted opposition). *Approach:*
lightly-curated, **quote/clip-driven** items drawn from soundbites + transcript spans across cities,
each linking the source meeting/timestamp; raw clips downloadable. *Tradeoff (voice):* lightly curated,
sourced — **expose the raw material** so groups take it further; never editorialize the factual record;
moderation/defamation care on anything characterizing named individuals.

**Substack newsletter channel (#18 email split).** *Problem:* reach beyond RSS. *Approach:* publish the
digests/highlights to **Substack** (external — avoids native email/PII/CAN-SPAM infra near-term); the
static digest is the source content. *Tradeoff:* some lock-in vs fast reach; keep RSS as the open mirror.

**Topic/region roll-up feeds + custom-query builder (#12/#13/#17).** *Problem:* "all zoning items in
TX," "my whole city." *Approach:* pre-generated combos (region/state/topic) as static feeds + **OPML**
export; a custom-query builder needs either pre-gen combinations or a Cloudflare Worker (Pages is
static). *Tradeoff:* combinatorial explosion → start with a curated set; depends on tags (#4).

**Privacy-respecting download analytics (GH#125).** *Approach:* OP3-style aggregate, self-owned
analytics-prefix subdomain; no per-user tracking. Informs which feeds/cities to invest in.

### §5.3 Phase F — Pre-Meeting Foresight

**Upcoming-agenda + staff-report scraping.** *Problem:* shift from after-the-fact to ahead-of-time.
*Approach:* extend providers with an `upcoming`/agenda capability (review/02 Change 8 capability set
already exists): Legistar/CivicClerk expose structured upcoming events + published files; Granicus/Swagit
need agenda-portal scraping. Persist as forward-looking records. *Tradeoff:* provider-dependent coverage;
strongest with Legistar (#31). The data spine for alerts + look-ahead.

**Upcoming-meetings `.ics` calendar (#19).** Generate per-city/body `.ics` from upcoming events; reuses
the fetch above. Static, low-cost.

**Watchlists + topic alerts (R8 extension).** *Problem:* "tell me when parking minimums hit an agenda."
*Approach:* match upcoming agenda items against topic tags (#4) → per-topic RSS first (no accounts),
email/Substack later. *Tradeoff:* RSS-first keeps it PII-free; precision depends on tag quality.

**Backup-material (packet) analysis.** *Problem:* the real decision detail is in staff reports/packets.
*Approach:* fetch the published packet (provider capability), extract text (PDF), and produce a
structured/LLM "what's being proposed" brief — cost-gated, cached, **additive + labeled**. *Tradeoff:*
PDF parsing + LLM cost; never overwrites official docs.

**Legistar provider (#31).** Rich InSite API: agendas, votes, rosters, upcoming events. Unlocks #8/#14
and high-quality foresight. *Approach:* standard new-adapter work + SSRF allowlist + fixtures.

**Vote/roll-call (#8) + attendee (#14) extraction.** From **platform metadata** (CivicClerk per-member
tallies) + scraped **released minutes** — **never inferred from audio**. Shared "minutes-ingestion"
component. ~$0 (parser).

### §5.4 Phase C — Co-Creation & AI Audio

**Look-ahead / look-back outlines & drafts.** *Problem:* lower the bar for residents/groups to publish.
*Approach:* generate a structured outline + draft (article or podcast script) from look-ahead foresight
(Phase F) and look-back digests (Phase E), with citations to meetings/timestamps; offered as
downloadable starting material. *Tradeoff:* LLM cost; drafts are clearly "starting points," sourced.

**AI-generated discussion podcast.** *Problem:* a listenable weekly show. *Approach:* compose an LLM
host script from the outline, interleave **real meeting audio clips** (via `clips.extract_clip` + EDL)
and TTS narration, render to an episode. *Tradeoff:* the most expensive + quality-sensitive item;
gated on revenue; the untrusted-output rule applies to all generated narration. The differentiator if it
works.

### §5.5 Cross-cutting / ongoing

**Strong Towns-focused discovery (#27/#32, rescoped).** *Problem:* grow toward where it helps most.
*Approach:* seed discovery from cities with active **Strong Towns Local Conversations**
(<https://www.strongtowns.org/local>), prevalidate against round-one provider traps (review/02 Change 9
security gate already exists), rank by group activity + archive quality — not population. *Tradeoff:*
list maintenance; aligns the catalog with the mission. Pairs with onboarding (#28).

**City-request + `/approve` onboarding (#28) · "report a feed problem" (#56) · auto-detect provider
(#30) · contributor scaffolding (#57).** The human-in-the-loop onboarding/health loop is
sketched here; issue templates + PR template **shipped**, label taxonomy (`area:*`, `needs-*`)
**shipped**, Projects board lands at 1.0.
Handoff docs (AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING) **shipped** with this doc set.

---

## §6. Deferred backlog (ongoing)

Items intentionally not in a near-term phase; revisit as scale or demand warrants. (Enumerated in §4
"Deferred backlog".) Notable rationale: **index sharding (#42)** is demoted because per-meeting pages
make meetings independently crawlable; the **DerivedArtifact refactor** (review/02 Change 5) waits until
a third artifact type exists (YAGNI); **full video / hosted DB / off-Actions media** are explicitly out
of scope now (§8). **Deleted:** #5 NER (the city's own document search is better ground truth).

---

## §7. Standing verification checklist (run on every update to this document)

Whenever this file, `VISION.md`, or `ROADMAP.md` changes, verify:

- [ ] **VISION ↔ this doc** — every long-horizon idea in VISION has a catalog entry; every Deferred
  entry is consistent with VISION.
- [ ] **ROADMAP ↔ this doc** — every committed near-term ROADMAP item has an L1+ catalog entry; statuses
  agree.
- [ ] **Catalog completeness** — every feature named in any doc (review/01–10, ROADMAP, PLAN, and open
  issues) is present, in a phase or the Deferred backlog.
- [ ] **Maturity/status consistency** — each entry's maturity (L0–L3) matches where its design lives
  (L1 inline / L2–L3 breakout) and reality (Shipped ⇒ merged PR).
- [ ] **CHANGELOG sync** — every Shipped entry has a CHANGELOG line; ARCHITECTURE.md updated if the
  architecture changed.
- [ ] **Breakout freeze** — every shipped initiative's breakout is stamped "Implemented in PR #N".
- [ ] **Cross-link integrity** — links across root docs ↔ this doc ↔ breakouts ↔ memory resolve.
- [ ] **Bump** the `Status: LIVING · last updated` date.

---

## §8. Codex review appraisal & post-review queue

The Codex review ([`review/10`](10-codex-architecture-throughput-roadmap-review.md)) was verified
against live code and is **accurate and well-grounded**. Disposition of its recommendations:

| Codex | Verdict | Home |
|---|---|---|
| R1 ASR benchmark → sharded split | Modify (benchmark CLI exists; need workflow + split) | H6/H9 |
| R2 stage backlog manifest | Adopt + **extend** with a configurable prioritization policy | H5 |
| R3 projection wall-clock fix | Adopt | H2 |
| R4 roadmap/docs reconciliation | Adopt (this doc set) | H1 |
| R5 feed-health catch-up states | Adopt | H4 |
| R6 feed-validation publish gate | Adopt | H3 |
| R7 per-meeting pages + search | Adopt | Phase R / review/13 |
| R8 Strong Towns tags/watchlists/alerts | Adopt (tags now; alerts → Phase F) | review/14 / §5.3 |
| R9 contributor/agent handoff guide | Adopt | shipped (AGENTS/ARCHITECTURE/CONTRIBUTING) |
| "defer email" | Modify → RSS/static first, **Substack** shortcut | Phase E |
| module splits | Adopt opportunistically (e.g. `ops/workqueue.py` from H5) | as touched |

**Post-review code queue (updated 2026-06-08 after H10 shipped in
[PR #232](https://github.com/BashfulBits/city-meeting-podcasts/pull/232)):** H8 resource guard →
H11a deploy resilience acceptance (native ASR/audio gate + one-slot audio lane) → H1 `gh` issue reconciliation — close/narrow GH#154
(`<podcast:transcript>` shipped), GH#110 (ASR → backfill/ops), GH#141 (timeline epic → umbrella only);
H2 projection wall-clock fix + tests; H3 validation gate; H4 feed-health states; H5 backlog manifest +
prioritization (including an explicit alignment-deferred lane for untimed provider transcripts);
H11b/H6 isolate enrich + sharded ASR, with separate align-only and transcribe-only lanes so stable-ts
and faster-whisper model loads do not stack in one runner; H9 offload evaluation. If H11a stays green
for several runs under the one-slot audio lane, do one near-term H11 tuning pass before H1–H5: evaluate
a 3-core ASR / 1-core audio-fetch-or-encode lane using heartbeat/gate logs, without reintroducing
multi-ffmpeg overlap.
When each step completes, apply §2's Implemented-row doc updates in the same PR or immediate post-merge
docs PR.

**Explicitly out of scope (now):** move to a hosted DB/API (no); move media off GitHub Actions (keep as
a fallback only); full video re-hosting (deferred — storage + legal surface). **Already satisfied:** live
endpoint contracts kept out of PR CI (separate `contracts.yml`).

---

## §9. Shared conventions (referenced by all breakouts)

Pointers, not copies — the source of truth for each:

- **Identity & invalidation** — stable UID, split hashes (`audio_spec_hash` vs `feed_content_hash`),
  content-addressed keys: `records.py`, [ARCHITECTURE.md](../ARCHITECTURE.md).
- **Append-only archive** — `records.merge_persisted` (#52).
- **Stage pipeline + stop budget** — audio-affecting stages precede `AudioStage`; gate only expensive
  restartable work; deferred ≠ failed: `stages.py` docstring, [ARCHITECTURE.md](../ARCHITECTURE.md).
- **Timeline served↔source basis** — [`review/08`](08-timeline-and-content-transforms.md),
  [`review/09`](09-infra-1-9-pr-audit.md).
- **Resource/throughput model** — [`review/03`](03-resource-model.md), `projection.py`.
- **Security** — SSRF gate, ffmpeg whitelist, defusedxml, **untrusted LLM output**:
  [SECURITY.md](../SECURITY.md), [`review/04`](04-audit-bugs-security.md), `security.py`.
- **Endpoint contracts / fixtures** — [`review/05`](05-endpoint-contract-tests.md), `contracts.yml`.
