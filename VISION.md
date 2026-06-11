# Vision

> The long-horizon north star. For what's being built **now**, see [ROADMAP.md](ROADMAP.md); for the
> design of any initiative named here, see [`review/11`](review/11-technical-design-roadmap.md).

## The problem

Local government meetings are nominally public and almost entirely inaccessible. The decisions that
shape a town — zoning, parking mandates, road widenings, budgets, debt, utility maintenance — are made
in long meetings, buried in proprietary video portals, with no transcript, no notification, and no
practical way to search or quote them. The people most affected almost never know an item is coming up
until it's already decided. Attendance is sparse, and the loudest voices in a half-empty room
disproportionately set policy.

## The thesis

This project makes local civic processes **legible and actionable**. It is built in the spirit of the
**Strong Towns** movement: the belief that financially resilient, incrementally built places come from
broad, informed local participation — not from a small number of people who happen to have time to sit
through a Tuesday-night meeting. If residents can hear, search, and share what their councils and
commissions actually do — and find out *before* a vote, not after — local advocacy gets dramatically
more effective.

## Who it's for

- **Strong Towns Local Conversation groups** (primary) — the local volunteer groups organizing for
  better land use and fiscal responsibility (ref: <https://www.strongtowns.org/local>). The tool exists
  to amplify their efficacy, and city onboarding prioritizes the places where these groups are active.
- **Civic activists, neighborhood organizers, and journalists** who need to monitor and quote meetings.
- **Engaged residents** who want a podcast of their city's meetings and a heads-up when something they
  care about is on the agenda.

## Editorial stance: lightly curated, raw material exposed

The platform has a point of view (the Strong Towns lens) but stays **sourced and quote-driven** — it
surfaces clips, transcripts, structured agenda/vote data, and topic framing, and lets readers draw
conclusions. Crucially, it **exposes the raw material** (downloadable clips, transcript timestamps,
agenda links, structured data, draft outlines) so that subscribers and local groups can take it as far
toward advocacy or editorial as *they* choose. The project curates; the community campaigns. The
factual record (titles, dates, votes, official links, transcript text) is never editorialized or
overwritten — including by AI.

## The three long-horizon directions (post-1.0)

These extend the project from "a podcast of past meetings" into a civic-engagement platform. They are
deliberately deferred until the research-tool core (per-meeting pages, transcript search, topic tags) is
solid and the project reaches 1.0.

### A. Engagement & distribution
Let people subscribe to the *project*, not just individual feeds. A "site news" RSS feed and a
newsletter (Substack-first for reach; RSS/static as the open foundation) carry auto-generated outlines
and articles about new features and, more importantly, **"national highlights"** from recent meetings:
examples of great public comment, council members doing the work well, and — transparently quoted —
what's being said out loud against housing and incremental development. The goal is to make land-use and
fiscal processes visible to a far wider audience than meeting-attendees.

### B. Pre-meeting foresight
Shift from after-the-fact to **ahead-of-time**. Scrape upcoming agendas and weekly staff reports to
council, match them against residents' interests, and notify subscribers when something they care about
is coming up — with analysis of the backup materials (staff reports, packets). This is where
participation actually changes outcomes.

### C. Co-creation & AI audio
Lower the bar for residents to act. Generate weekly **look-ahead** and **look-back** outlines and drafts
that a resident or local group can turn into their own article or podcast. Optionally, produce an
AI-generated podcast where hosts discuss the key items and **pull in real meeting audio** to show how
things actually happened — built on the existing clip/EDL + transcript + LLM machinery. This becomes a
genuine differentiator *if and when* revenue supports the generation cost.

## How we get there

- **Depth-first.** Prove the rich research-tool features across the **entire current city catalog**
  before onboarding new cities. The "focused set" is today's roster — breadth (many more cities) comes
  *after* per-meeting pages, transcript search, and topic tags are solid on the cities already covered.
- **Discovery follows the movement.** Onboard human-requested cities as they come, and target discovery
  at cities with active Strong Towns Local Conversations rather than raw population rank.
- **Hardening & efficiency first.** Stabilize and maximize throughput of the (free) pipeline before
  layering on new product surface.
- **AI used judiciously.** Stay near $0 cash in the near term (self-hosted transcription on free CI);
  spend on high-value generation as donations/subscriptions grow with the catalog.
- **Compute is pluggable.** Heavy inference — transcription, forced alignment, and later diarization /
  AI audio — runs behind a single **execution-backend interface**: the free GitHub Actions runner today,
  with Modal / Kaggle / a self-hosted Mac-mini runner / AWS as drop-in backends as the catalog grows. The
  goal is that scaling compute post-1.0 means writing **one adapter**, never rearchitecting the pipeline
  logic — the same shape as the existing pluggable storage backend. This interface is a **pre-1.0 lock**.

## Sustainability & monetization

The public meeting record stays **free and unpaywalled, always.** Funding comes first from
**donations and grants** (`<podcast:funding>`, privacy-respecting aggregate analytics). A **freemium**
tier (premium convenience: custom alerts, personalized digests, AI-podcast generation — never the raw
record) is on the table only if the project grows enough to sustain it as a viable ongoing effort.
Features are designed for that optionality without ever gating public information.

## What success looks like

A resident in any covered city can: subscribe to their council as a podcast; search every meeting by
keyword; open a shareable page for any meeting with player, transcript, agenda, and source-time links;
get a heads-up (with analysis) when a topic they watch is on next week's agenda; and hand a local group
a ready-to-use outline for an article or episode. Strong Towns Local Conversations use it as
infrastructure. The factual record stays trustworthy; the advocacy is the community's own.
