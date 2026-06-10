# Roadmap

Public, maintainer-curated **near-term** direction: converting US city-meeting archives into
subscribable podcast feeds + a searchable, civic-research directory. Priorities use a **0 = highest**
scale; lower number = sooner.

> **Status:** beta, single-maintainer development. Issues are filed **just-in-time** for the current
> working set rather than all at once. This file is **forward-looking**; the history of what shipped
> lives in [CHANGELOG.md](CHANGELOG.md), the long-horizon direction in [VISION.md](VISION.md), and the
> full design + the exhaustive backlog in **[`review/11`](review/11-technical-design-roadmap.md)**.
> The backlog opens for outside contribution after **1.0** (see [CONTRIBUTING.md](CONTRIBUTING.md)).

## Recently shipped (summary)
Timeline/EDL foundation; **#52** append-only content permanence; audio-cleanup band (**#22** silence
trim, **#21** loudness, **#23** host-all, **#122** concat, clips); **#1/#110** ASR transcripts (reuse
provider transcripts first, self-host the rest); **#11** `<podcast:transcript>`; **#124** status
dashboard; durable bucket-backed state; SSRF gate; feed-health audit; endpoint contracts; resource
projection + admin page. Full history → [CHANGELOG.md](CHANGELOG.md).
Recently shipped Phase H reliability work: **H10** ASR alignment fallback fix (PR #232), **H8**
runner resource guard (PR #235), and **H11a** deploy resilience — native work gate + one-slot audio
lane + concurrency tuning + Retry-After fix (PRs #239/241/242/243/244/246/247).

## Current phase: **H — Hardening & Efficiency** (next up)
Stabilize and maximize the throughput of what just shipped *before* layering on new user-facing
features. Detailed design: [`review/12`](review/12-hardening-and-efficiency.md).

> **Reprioritized 2026-06-08** after a build-log root-cause review: **H10 shipped in PR #232** and
> **H8 shipped in PR #235**; the remaining do-now reliability item **H11a** runs **ahead of H1–H5**.
> These fixes address what is turning Build & Deploy red on ~half of scheduled runs (runner
> starvation; H10 fixed the broken ASR `align` path). See
> [`review/12`](review/12-hardening-and-efficiency.md#build-log-root-cause-analysis-2026-06-08).

| Pri | Item |
|----:|------|
| **H1** | Docs/roadmap/issue reconciliation (this doc set; close/narrow shipped issues) |
| **H2** | Projection wall-clock fix — drop the legacy `materialize_budget_per_run` default; add audio + transcript backlog rows |
| **H3** | **#53** feed-validation publish gate in `deploy.yml` |
| **H4** | Feed-health **catch-up vs stalled** states + ETA + auto-comment (untangle the issue sprawl) |
| **H5** | Stage **backlog manifest** + a configurable, extensible **prioritization policy** (recency / city order / feed-visible-first / requested-first / …) |
| **H6** | ASR **benchmark workflow** → **sharded/separate ASR workflow** (after safe state coordination) |
| **H7** | ✓ Shipped — contributor/agent handoff docs (AGENTS/CLAUDE/ARCHITECTURE/CONTRIBUTING + PR/issue templates) |
| **H8** | ✓ Shipped — throughput maximization on the free 4-core runner (PR #235): pinned ffmpeg `-threads`, memory/CPU admission guard, abandoned-ASR-thread accounting |
| **H9** | Evaluate **free transcription-offload tiers** (matrix sharding first; then free ASR-API/compute, ToS-checked) |
| **H10** | ✓ Shipped — ASR alignment fix (PR #232): caption-bearing feeds use a stable-ts align model and fall back to fresh transcription on align errors |
| **H11a** | ✓ Shipped — **Deploy resilience**: native work gate + one-slot audio lane + concurrency tuning + Retry-After fix (PRs #239/241/242/243/244/246/247) |
| **H11b** | After H5 — isolate enrich into its own workflow (own concurrency group, separate from Pages deploy) |

## Toward 1.0: **R — Research-Tool Surface**
Turn feeds into a civic-research tool. Design: [`review/13`](review/13-per-meeting-pages-and-search.md)
(pages + search) and [`review/14`](review/14-topic-tags-strong-towns-lens.md) (tags).

| Pri | Item |
|----:|------|
| **R1** | **#46/#157** per-meeting permalink pages (player, transcript, chapters/agenda, official + source-time links, shareable deep-links, "report a problem") |
| **R2** | **#6** static client-side transcript search |
| **R3** | **#4** topic tags / **Strong Towns lens** (transparent rules + human overrides; LLM-assist later) |
| **R4** | **#3** per-agenda-item "what changed" cards · **#2** auto-summaries · **#15** soundbites |
| **R5** | **#55** front-end design cycle · **#50** accessibility · **#16** funding link |

## 1.0 milestone (drop the beta tag)
Phase **H** green + **#52** content permanence (shipped) + **#53** validation gate (H3) + **#55**
front-end design cycle + **#50** accessibility.

## Beyond 1.0 (the long-horizon phases)
Documented in [VISION.md](VISION.md); designed at sketch level in [`review/11`](review/11-technical-design-roadmap.md):
- **Phase E — Engagement & Distribution**: site-news RSS + Substack newsletter, weekly look-back digest,
  "national highlights", **#13** roll-up feeds, **#17** OPML, **#12** custom-query feed builder.
- **Phase F — Pre-Meeting Foresight**: upcoming agendas + weekly staff reports, **#19** `.ics` calendar,
  watchlists + topic alerts, **#31** Legistar (rich agendas/votes), backup-material analysis.
- **Phase C — Co-Creation & AI Audio**: look-ahead/look-back outlines & drafts, AI-generated podcast.

## Cross-cutting
- **Discovery (#27/#32) targets Strong Towns Local Conversation cities** (<https://www.strongtowns.org/local>)
  and human-requested cities (**#28/#56**) — not raw population rank.
- **Monetization**: free + donations/grants (**#16**, **#125** OP3 analytics); freemium considered only
  if the project grows enough to sustain it; never paywall the public record.
- **Security**: LLM output is untrusted; SSRF gate on any user-submitted source (see [SECURITY.md](SECURITY.md)).
- **Deferred backlog**: speaker diarization (#7), translation (#9), bitrate ladders (#24), chapter
  images (#26), "new since last visit" (#48), full video re-hosting, hosted DB/API, off-Actions media.

## How priorities work here
Items are scoped, rationalized, and cost-modeled in [`review/11`](review/11-technical-design-roadmap.md)
(which links the deep breakout designs). The maintainer drives sequencing; once contribution opens
(1.0), well-scoped low-cost items get labeled **good first issue**.
