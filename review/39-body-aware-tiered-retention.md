# Body-aware tiered retention

**Status: L3 development-ready · implementation in `feat/body-aware-tiered-retention` (2026-07-26)**

## Decision and deviation record

The prior plan deferred archive backfill: it retained a source-wide record window and materialized
only the feed-visible subset. On 2026-07-26 the maintainer explicitly promoted a bounded version of
that work. The benefit is coherent public history for every body that shares a provider listing; the
cost is gradual audio/ASR backfill and a larger retained state. The alternative—leaving the source-wide
5,000-record cap—lets a busy body evict quieter boards and makes the three public retention promises
impossible to state accurately.

## Locked policy

All configured feeds inherit these limits from `config/site_config.yml` → `defaults`; historical
per-feed `max_episodes: 25|50` overrides are removed. The loader rejects all feed-level retention
keys (`max_episodes`, `full_artifact_episodes`, `metadata_retention_episodes`, and the retired
`max_archive_items`) so an old override cannot silently create a conflicting policy.

| Per canonical body rank | Policy |
|---:|---|
| 1–500 | Publish in RSS and the feed landing page; materialize normally. |
| 501–2,000 | Do not publish in RSS; gradually materialize and retain audio plus every artifact. |
| 2,001–10,000 | Retain episode/calendar metadata and non-audio artifacts, including transcripts and downloaded-document text; remove hosted-audio pointers so normal orphan GC can reclaim audio. |
| 10,001+ | Prune the durable record. |

The canonical store remains one `episodes.json` per `source_key`, but the retained set is the **union**
of the independent body windows. A body-less combined feed still publishes its newest 500 overall;
body-specific feeds and work selection use their own body ranks. The source key must continue to ignore
the feed's body filter: duplicating records/audio per feed would break migration identity and content
addressing.

## Implementation plan

1. Add site-wide YAML `full_artifact_episodes` (2,000) and `metadata_retention_episodes` (10,000)
   beside the existing feed-visible `max_episodes` (500), validating the monotonic relationship and
   rejecting feed-level overrides.
2. Replace production source-wide archive projection with a per-body merge → age-filter → rank → union
   projection. Demote only the `audio` block in the metadata-only tier; preserve non-audio blocks and
   their referenced objects.
3. Apply the same metadata tier to calendar-only rows. Keep the legacy source-wide projection callable
   only for external compatibility/tests, not production configuration.
4. Expand enrichment selection to 2,000 per body, activate `recent_archive`, and place
   `feed_visible_first` ahead of the gradual archive cohort under the existing wall-clock stop budget.
   This is a gradual backfill: there is no pipeline-version bump and no forced re-encode of already
   hosted audio.
5. Keep RSS and feed landing pages at 500. Archive pages and search read the retained metadata tier.
   Normal object GC reclaims demoted audio because the audio key is removed from the canonical record;
   transcript/document keys remain live.
6. Update reports, architecture, config, and tests for multi-body independence, audio demotion, and
   active archive work.

## Acceptance criteria

- A source with two bodies retains 10,000 rows for each where available; one body's volume cannot evict
  the other's retained history.
- Rank 501 materializes eventually but never appears in RSS; rank 2,001 retains transcript/document
  pointers but no audio pointer; rank 10,001 is absent.
- The 500 feed-visible cohort is scheduled before the 501–2,000 cohort.
- Existing audio below rank 2,000 keeps its content-addressed key; no pipeline-version bump or blanket
  backfill is introduced.
- Per-feed legacy 25/50 overrides no longer constrain production feeds.
