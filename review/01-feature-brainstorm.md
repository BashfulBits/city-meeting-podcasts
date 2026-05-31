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

## Recommended sequencing

1. **Now (cheap, high-leverage, no new deps):** #29 (security gate, before any Phase-5 work), #35/#36/#37
   (ops visibility), #38, #41, #43, #11, #46.
2. **Next paid stages (need a key + budget):** #1 transcripts → unlocks #2, #3, #5, #6, #8, #9, #11.
   Transcripts are the keystone — most A-group features derive from them.
3. **Coverage:** #31 Legistar + #27/#28 discovery, once #29 is in.
4. **Polish:** the rest as bandwidth allows.

The keystone insight: **transcripts unlock ~10 downstream features**, so the transcript stage's
storage/cost model (doc 03) is worth modeling carefully before committing.
