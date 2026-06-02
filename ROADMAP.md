# Roadmap

Public, maintainer-curated direction for this project (converting US city meeting archives into
subscribable podcast feeds + a searchable directory). Priorities use a **0 = highest** scale.

> **Status:** beta, single-maintainer development. Issues are filed **just-in-time** for the
> current working set rather than all at once. The full prioritized backlog lives here; it opens up
> for outside contribution after **1.0** (see [CONTRIBUTING.md](CONTRIBUTING.md)). Detailed rationale,
> cost models, and per-item notes are in [`review/01-feature-brainstorm.md`](review/01-feature-brainstorm.md).

## Shipped
Stable identity + content-addressed audio refactor; durable bucket-backed build state; enrichment-stage
pipeline (audio, chapters, resource/agenda links); Podcasting 2.0 `<podcast:chapters>`; resource
cost/time projection + admin page; endpoint contract tests; SSRF source-URL gate; feed-health audit.

## 1.0 milestone (drop the beta tag)
The launch-gating set: **#52** content permanence, **#53** feed-validation publish gate, **#55**
front-end design cycle, **#50** accessibility.

## Prioritized backlog
| Pri | Items |
|----:|-------|
| **0.5** | #52 content permanence + feed-health (append-only archive; never silently drop old meetings) |
| **1**   | #1 ASR transcripts (reuse provider transcripts first, self-host the rest) · #22 silence-trim / timeline-transform · #51 official meetings-page link |
| **1.5** | #11 `<podcast:transcript>` · #21 loudness normalization · #23 host-all-audio |
| **2**   | #2 auto-summaries · #3 per-agenda-item summaries · #16 funding link · #28 city onboarding (`/approve`) · #30 auto-detect provider · #46 per-meeting permalink pages · #53 feed-validation publish gate |

<!-- #28 note: city onboarding flow must populate `meetings_url` in the generated YAML. The value
     should be the city's own meetings/agenda-portal page (not the provider URL). At onboard time
     this can be sourced from: (a) the submitter's issue, (b) a web search for
     "<city name> agendas minutes site:<city_website domain>", or (c) a provider-specific heuristic
     as a last resort. The field is optional — fall back to city_website on render — but should be
     populated whenever discoverable so every episode carries a ground-truth link. -->
| **2.5** | #15 soundbite highlights · #55 front-end design cycle (subscribe-button iconography, index redesign) |
| **3**   | #4 topic tags · #6 full-text search · #18 newsletter (RSS-first) · #25 intro/outro stinger · #31 Legistar provider · #41 backlog catch-up auto-rebalance · #50 accessibility · #56 user "report a feed problem" template |
| **3.5** | #19 upcoming-meetings calendar (.ics) · #49 admin dashboard |
| **4**   | #8 vote extraction (platform metadata + minutes) · #12 custom query feed builder · #14 attendee extraction · #17 OPML export · #27 population-ranked discovery · #32 scheduled board-feed generation · #33 dead-city archival · #39 per-provider rate limiting · #40 actual-cost dashboard · #42 index sharding · #57 contributor scaffolding (do at 1.0) |
| **5**   | newsletter email delivery · #31 YouTube + other providers · per-feed config via issue comments · structured logging · map browser · #122 audio concat for multi-segment legacy Swagit meetings (single-segment fallback already ships in #120; multi-segment meetings are detected and deferred via materialization backoff until ffmpeg concat lands) |
| **deferred** | speaker diarization · translation · bitrate ladders · chapter images · "new since last visit" |

## How priorities work here
Items are scoped, rationalized, and cost-modeled in `review/01`. Lower number = sooner. The
maintainer drives sequencing; once contribution opens (1.0), well-scoped low-cost items will be
labeled **good first issue**.
