# Feature Brainstorm (50 candidates)

Grouped by theme. Each: **Value** (listener/maintainer impact), **Effort** (S/M/L/XL),
**Cost** (ongoing $), and a one-line note. "Stage" = fits the existing enrichment-stage pipeline
with no structural change.

Legend — Effort: S ≤½ day · M ~1–2 days · L ~a week · XL multi-week.
Cost: 🆓 none · 💲 small API/compute · 💲💲 material.

## A. Episode content & discoverability (listener-facing)

1. **ASR transcripts (Whisper/Deepgram)** — Value: high · Effort: L · 💲💲. Stage; store transcript
   text + link in notes; the single biggest accessibility + SEO win. Cost model in doc 03.
2. **AI auto-summaries** — Value: high · Effort: M · 💲. Stage (feed-only). 3–5 sentence "what
   happened" from transcript or agenda. Needs an LLM key.
3. **Per-agenda-item summaries** — Value: high · Effort: L · 💲. Combine chapter timestamps +
   transcript spans → a summary per agenda item, rendered as chapter descriptions.
4. **Keyword/topic tags per episode** — Value: med · Effort: M · 💲. Enables topic feeds (#12).
5. **"Mentioned in this meeting" entities** — Value: med · Effort: L · 💲. NER over transcript:
   people, addresses, dollar amounts, ordinances. Great for journalists.
6. **Full-text search across all meetings** — Value: high · Effort: L · 🆓. Static client-side
   index (e.g. Pagefind/MiniSearch) built from transcripts; no server.
7. **Speaker diarization + labels** — Value: med · Effort: L · 💲💲. "Council Member X said…".
8. **Vote/roll-call extraction** — Value: high (civic) · Effort: M · 🆓 for CivicClerk/Granicus
   (already in their JSON), 💲 elsewhere. Structured "how they voted" per item.
9. **Translated transcripts/summaries (ES first)** — Value: high (TX) · Effort: M · 💲. Per-language
   alternate feeds or notes.
10. **Chapter descriptions/links from agenda packet** — Value: med · Effort: M · 🆓. Link each
    chapter to its agenda-packet page.

## B. Feed/podcast-platform features

11. **Podcasting 2.0 `<podcast:transcript>`** — Value: high · Effort: S · 🆓. Once transcripts exist,
    reference them so apps show synced transcripts. (Chapters already done.)
12. **Topic/issue feeds** — Value: med · Effort: M · 🆓. "All zoning items statewide" cross-cuts via
    tags (#4). Differentiator.
13. **Per-state / per-region roll-up feeds** — Value: med · Effort: S · 🆓. Aggregate feed.
14. **`<podcast:person>` / `<podcast:location>` tags** — Value: low-med · Effort: S · 🆓.
15. **`<podcast:soundbite>` highlights** — Value: med · Effort: M · 💲. Auto-pick a 60s clip.
16. **Funding/`<podcast:funding>` + value-for-value** — Value: low · Effort: S · 🆓.
17. **OPML export of all feeds** — Value: med · Effort: S · 🆓. One-click "subscribe to my whole city".
18. **Email/RSS-to-newsletter digest** — Value: med · Effort: M · 💲. Weekly "what your council did".
19. **Calendar (.ics) of upcoming meetings** — Value: med · Effort: M · 🆓–💲. Most providers expose
    upcoming events (CivicClerk `Events`, Granicus). Re-uses fetch.
20. **Video feeds where source is progressive MP4** — Value: low-med · Effort: S · 🆓 (already
    partially supported; expand).

## C. Audio quality & processing

21. **Loudness normalization (EBU R128)** — Value: med · Effort: S · 🆓 (ffmpeg loudnorm). Council
    audio levels are wildly inconsistent.
22. **Silence/dead-air trimming** — Value: med · Effort: M · 🆓. Trim long pre-meeting silence.
23. **Host-all-audio (re-host even when a direct MP4 exists)** — Value: med · Effort: S · 💲💲 storage.
    Consistency + chapter embedding for Granicus. **Explicitly a projection knob (doc 03).**
24. **Multiple bitrate ladders** — Value: low · Effort: M · 💲💲. Probably not worth it.
25. **Intro/outro stinger** — Value: low · Effort: S · 🆓. Brand each episode; could state the city.
26. **Chapter images** — Value: low · Effort: M · 💲. From agenda packet pages.

## D. Scale, discovery & onboarding (Phase 5)

27. **Population-ranked provider discovery** — Value: high · Effort: L · 🆓. The Phase 5 core.
28. **Per-entity `city-request` GitHub issues + `/approve`** — Value: high · Effort: M · 🆓.
    Human-in-the-loop onboarding (already designed).
29. **Source-URL allowlist/validation gate** — Value: high (security) · Effort: M · 🆓. Prereq for #28
    (audit #S1).
30. **Auto-detect provider from a city URL** — Value: med · Effort: M · 🆓. Paste a city site → guess
    Granicus/Swagit/etc.
31. **More providers: Legistar, YouTube (gov channels), Vimeo, Zoom, BoxCast, Cablecast** — Value:
    high (coverage) · Effort: L each · 🆓–💲. Legistar especially (rich structured agendas).
32. **Auto-body-feed generation on a schedule** — Value: med · Effort: M · 🆓. Re-run
    generate_board_cities periodically to catch new boards.
33. **Dead-city detection + archival** — Value: med · Effort: S · 🆓. Auto-flag feeds gone silent
    (the audit half-does this).
34. **Per-feed config overrides via issue comments** — Value: low · Effort: M · 🆓.

## E. Reliability, ops & cost (maintainer-facing)

35. **Resource usage monitor + projection** — Value: high · Effort: L · 🆓. **Doc 03.**
36. **Run-history / trend metrics** — Value: med · Effort: M · 🆓. Append per-run JSON (backlog depth,
    budget spent, errors) → trend, not snapshot.
37. **Endpoint contract tests + monitor** — Value: high · Effort: M · 🆓. **Doc 05.**
38. **Fetch retry/backoff** — Value: med · Effort: S · 🆓. Cuts false-positive health issues.
39. **Per-provider rate limiting / politeness budgets** — Value: med · Effort: M · 🆓. Avoid being
    blocked at scale.
40. **Storage cost dashboard from real B2 usage API** — Value: med · Effort: M · 🆓. Reconcile
    projection vs actuals.
41. **Backlog "catch-up mode"** — Value: high · Effort: S · 🆓. Auto-raise budgets when feature
    rollout is quiet so the 6h window is fully used (doc 03 §6).
42. **Index sharding by region** — Value: med · Effort: M · 🆓. The directory JSON doesn't scale past
    a few hundred feeds.
43. **Alias/slug collision validation** — Value: med · Effort: S · 🆓 (audit #B2).
44. **Structured logging + a build report artifact** — Value: med · Effort: S · 🆓.
45. **Self-healing dead-enclosure re-resolve** — Value: med · Effort: M · 🆓. When audit finds a dead
    Granicus link, re-fetch the feed to pick up the new URL.

## F. Web / directory UX

46. **Per-meeting permalink pages** (with embedded player + transcript + chapters) — Value: high ·
    Effort: M · 🆓. Currently only a feed item; a real web page per meeting is huge for SEO/sharing.
47. **Map-based city browser** — Value: med · Effort: M · 🆓.
48. **"New since you last visited" / recent activity** — Value: low · Effort: S · 🆓.
49. **Admin dashboard (cost, backlog, health, projection)** — Value: high (maintainer) · Effort: M ·
    🆓. **Doc 03 admin page.**
50. **Accessibility pass on the site (WCAG)** — Value: med · Effort: S · 🆓.

## Maintainer prioritization (decided 2026-05-31)

Walked the full list with the maintainer and assigned priorities (**0 = highest**; lower = sooner).
This section is authoritative and supersedes the original "recommended sequencing" guess.

### New items added during the walkthrough
- **#51 — Official city meetings-page link** (P1). Add each city's canonical meetings/agenda-portal
  URL to every episode's notes + page (new optional `meetings_url`, fallback `city_website`); a
  one-line extension of `LinksStage`. $0.
- **#52 — Content permanence + feed-health triage** (**P0.5 — do first**; absorbs #45). **Correctness gap:** the
  record store is currently *replaced* each run with only freshly-fetched episodes, so content that
  drops off a provider feed (Granicus 100-item cap, Swagit windowing) is *lost*. Make the archive
  **append-only** (accumulate records; render feeds from the full store), which also stops orphan-GC
  from reaping archived audio. Then rework feed-health detection to separate **(a) pending backlog**
  (suppress), **(b) provider-dropped-but-archived** (expected), **(c) genuine regression** (file an
  actionable ticket), and add self-healing dead-enclosure re-resolve (former #45). The append-only
  archive sub-part is a silent-content-loss risk, hence P0.5.
- **#53 — Feed validation as a publish gate** (P2, sequenced at the *tail* of the P0.5–2 band — see
  Adjustments). `citypods/validate.py` already does structural validation in CI; promote it to a
  `deploy.yml` gate (malformed feed never publishes) + richer iTunes/Podcasting-2.0 checks.
- **#55 — Front-end design cycle** (P2.5; absorbs #20 + #54). Iterative mockup-driven redesign of the
  index (accordion that *looks* foldable; explain/justify any pre-expanded cities), subscribe-button
  **app iconography**, and clear **audio-vs-video** labeling. Coordinate in time with **#46**.
- **#56 — User-facing "report a problem with this feed/city" issue template + triage** (P3). New-city
  requests are already covered by #28; this adds the feed/city *problem-report* path. Pairs with #52.

### Deletes / defers / merges / rescopes
- **Deleted:** #5 (entities/NER) — the city's own document search is better ground truth at that detail.
- **Deferred (backlog, unlikely-soon):** #7 (speaker diarization — high complexity, low value),
  #9 (translation), #24 (bitrate ladders), #26 (chapter images), #47 (map browser), #48 ("new since
  last visit" — podcast clients do this; avoid storing user data beyond donations).
- **Merged:** #12+#13 → **custom query feed builder** (pick region/state/location/topic → personalized
  RSS; needs pre-gen combos or a Cloudflare Worker since Pages is static). #20+#54+#55 → front-end
  design cycle. #45 → #52.
- **Rescopes:**
  - **#1** — reuse provider-supplied transcripts (Swagit `/videos/{id}/transcript`, CivicClerk
    `transcriptionUrl`/`.srt`) *before* running ASR → ~57% of current feeds get free transcripts;
    self-host ASR (faster-whisper on Actions) for the rest.
  - **#8 (votes)** — harvest **platform metadata** (CivicClerk per-member tallies) + scrape the
    **released minutes** documents; **never infer from audio** (electronic voting leaves nothing in
    the recording). Coverage: full CivicClerk, partial Granicus, none Swagit/CivicPlus.
  - **#14** — attendees/people come from the **minutes documents** (shared "minutes ingestion"
    component with #8), not audio diarization.
  - **#22** — silence-strip and provider transcripts are **not** exclusive: define one per-episode
    **timeline transform** (silence cut-map) and remap chapters + transcript onto the served audio;
    generate ASR post-trim only when no transcript exists. Develop jointly with #1.
  - **#18** — RSS-delivered digest first (a donor perk); email delivery split out (P5).

### Already shipped (this review)
#29 SSRF gate (#108), #35 monitor/projection (#107), #36 run-history (#107), #37 endpoint contract
tests (#105), #38 retry/backoff (#105), #43 alias validation (#105). Partial: #41 (time-bounded
budgets shipped, auto-rebalance remaining), #49 (projection page shipped, dashboard extension open).

### Priority-sorted roadmap
| Pri | Items |
|----:|-------|
| **0.5** | #52 content permanence + feed-health (don't silently lose old content — append-only archive) |
| **1**   | #1 transcripts (reuse-first) · #22 silence-trim/timeline-transform · #51 meetings link |
| **1.5** | #11 `<podcast:transcript>` (starts only after #1) · #21 loudness norm · #23 host-all-audio |
| **2**   | #2 summaries · #3 per-item summaries · #16 funding link · #28 onboarding issues+/approve · #30 auto-detect provider · #46 per-meeting pages · #53 feed-validation gate (lands at the *tail* of this band, once the feed shape settles) |
| **2.5** | #15 soundbites · #55 front-end design cycle (incl. #20/#54) |
| **3**   | #4 tags · #6 full-text search · #18 newsletter (RSS-first) · #25 intro/outro stinger · #31 **Legistar** · #41 catch-up auto-rebalance · #50 accessibility · #56 user feed-problem reports |
| **3.5** | #19 upcoming .ics · #49 admin dashboard |
| **4**   | #8 votes (metadata+minutes) · #12+#13 custom feed builder · #14 attendees (minutes) · #17 OPML · #27 population-ranked discovery · #32 scheduled board-gen · #33 dead-city archival · #39 per-provider rate limiting · #40 B2 actual-cost dashboard · #42 index sharding |
| **5**   | #18-email expansion · #31 YouTube + other providers · #34 config via issue comments · #44 structured logging · #47 map browser |
| **defer** | #7 diarization · #9 translation · #24 bitrate ladders · #26 chapter images · #48 new-since-visit |
| **deleted** | #5 entities/NER |
| **done** | #29 · #35 · #36 · #37 · #38 · #43 (partial: #41, #49) |

### Adjustments (round 2, 2026-05-31)
- **#52 → 0.5** (do first): the append-only archive is a silent-content-loss fix; everything else
  assumes we never drop old meetings.
- **#11 → 1.5**: strictly depends on #1, so it can't *start* until transcripts are finished.
- **#53 → 2, placed at the *tail* of the P0.5–2 band**: the band changes the feed's shape
  (transcripts, restructured items, append-only), so a publish-gate built *earlier* would be a
  moving target — and CI snapshot/structural validation already guards the feature work pre-merge.
  #53's distinct value is a `deploy.yml` gate catching production-only/real-data regressions, which
  only pays off once the shape **stabilizes** (and before scaling to hundreds of feeds). So: P2 by
  importance, but *sequenced after* #1/#52/#46 land.
- **#16 → 2** (trivial; donations fund the rest). **#25 → 3** (polish + annoyance risk).
  **#42 → 4** (#46 makes meetings independently crawlable, dropping sharding urgency at ~80 feeds).
- **#8 left at 4** (maintainer call) despite being a cheap, transcript-free civic win.

### Keystone
**Transcripts (#1) unlock ~8 downstream features** (#2/#3/#6/#11/#15, plus better #8/#14 via minutes),
so model their storage/throughput first (doc 03) — but the reuse-first rescope means only ~34 feeds
(mostly Granicus) need ASR, roughly halving the backlog. The cheapest high-value win that needs no
transcripts and ~$0 is **#51 (meetings link)**; #16 (funding) and the foundation of #41 (just enable
`run_time_budget_minutes`) are similar near-free do-now actions.
